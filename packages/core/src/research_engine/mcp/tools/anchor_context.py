"""anchor_context tool — show what surrounds a verified quotation."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog

from research_engine.domain.errors import NotFoundError
from research_engine.mcp.errors import envelope, failed

logger = structlog.get_logger()

TOOL_NAME = "anchor_context"
TOOL_DESCRIPTION = (
    "Read canonical text around a ledger anchor, every anchor on a claim, a "
    "work-file citation id, or a bare source span. The returned offsets mark "
    "the quotation inside the window. Verification proves characters exist; "
    "this context is what lets a reader assess whether the claim uses them "
    "faithfully. Pass exactly one selector."
)
TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "anchor_id": {"type": "string", "format": "uuid"},
        "claim_ref": {"type": "string"},
        "citation_id": {"type": "string"},
        "span_id": {"type": "string", "format": "uuid"},
        "window": {"type": "integer", "minimum": 0, "default": 1200},
    },
}


async def handler(
    container: Any,
    *,
    anchor_id: str | None = None,
    claim_ref: str | None = None,
    citation_id: str | None = None,
    span_id: str | None = None,
    window: int = 1200,
) -> dict[str, Any]:
    given = [
        name
        for name, value in (
            ("anchor_id", anchor_id),
            ("claim_ref", claim_ref),
            ("citation_id", citation_id),
            ("span_id", span_id),
        )
        if value is not None
    ]
    if len(given) != 1:
        return envelope(
            "invalid_input",
            "Pass exactly one of anchor_id, claim_ref, citation_id, or span_id "
            f"(got {given}).",
            None,
        )
    if isinstance(window, bool) or not isinstance(window, int) or window < 0:
        return envelope("invalid_input", "window must be a non-negative integer", None)

    try:
        if span_id is not None:
            context = await container.anchor_context_service.for_span(UUID(span_id), window)
            return {"contexts": [context.model_dump(mode="json")]}
        if anchor_id is not None:
            anchor = await container.claims.anchor_by_id(UUID(anchor_id))
            if anchor is None:
                raise NotFoundError("anchor", anchor_id)
            context = await container.anchor_context_service.for_span(
                anchor.source_span_id, window
            )
            return {
                "contexts": [
                    {
                        "anchor_id": str(anchor.id),
                        **context.model_dump(mode="json"),
                    }
                ]
            }
        if claim_ref is not None:
            claim = await container.claims.get_by_ref(claim_ref)
            if claim is None:
                raise NotFoundError("claim", claim_ref)
            anchors = await container.claims.anchors_for(claim.id)
            contexts = await _anchor_contexts(container, anchors, window)
            return {"claim_ref": claim_ref, "contexts": contexts}

        reader = getattr(container, "work_files", None)
        if reader is None:
            return envelope(
                "works_not_configured",
                "RE_WORKS_DIR is not set, so no work citation can be read.",
                None,
            )
        matches = []
        for work_path in reader.list_works():
            work = reader.read(work_path)
            for entry in work.front_matter.citations:
                if entry.id == citation_id:
                    matches.append((work.work_path, entry))
        if not matches:
            raise NotFoundError("work citation", citation_id)
        contexts = await container.anchor_context_service.many_for_coordinates(
            [
                (entry.document_id, entry.char_start, entry.char_end)
                for _, entry in matches
            ],
            window=window,
        )
        return {
            "contexts": [
                {
                    "citation_id": citation_id,
                    "work_path": work_path,
                    **context.model_dump(mode="json"),
                }
                for (work_path, _), context in zip(matches, contexts, strict=True)
            ]
        }
    except (ValueError, TypeError) as exc:
        return envelope("invalid_input", str(exc), None)
    except NotFoundError as exc:
        return envelope("not_found", str(exc), None)
    except Exception as exc:  # noqa: BLE001 - unexpected tool failure envelope
        logger.error("anchor_context_error", error=str(exc))
        return failed(TOOL_NAME, exc)


async def _anchor_contexts(container: Any, anchors: list[Any], window: int) -> list[dict[str, Any]]:
    contexts = await _span_contexts(
        container, [anchor.source_span_id for anchor in anchors], window
    )
    return [
        {"anchor_id": str(anchor.id), **context}
        for anchor, context in zip(anchors, contexts, strict=True)
    ]


async def _span_contexts(
    container: Any, span_ids: list[UUID], window: int
) -> list[dict[str, Any]]:
    contexts = []
    for source_span_id in span_ids:
        context = await container.anchor_context_service.for_span(source_span_id, window)
        contexts.append(context.model_dump(mode="json"))
    return contexts
