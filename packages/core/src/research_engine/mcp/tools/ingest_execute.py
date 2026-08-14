"""ingest_execute tool -- act on IngestActions produced by search_sources.

``search_sources`` is read-only: it returns matches carrying an
``ingest_action`` ({tool, args}) but never executes them. This tool closes the
loop — feed it a search_sources result (or an explicit action list) and it
dispatches each action through the normal tool machinery, skipping matches that
are already in the corpus. One round-trip from a reading list to ingestion.
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger()

TOOL_NAME = "ingest_execute"
TOOL_DESCRIPTION = (
    "Execute ingest actions from a search_sources result (or an explicit list). "
    "Pass `matches` (the `matches` array from search_sources) and/or `actions` "
    "([{tool, args}]). Each action is dispatched through its target tool; "
    "matches already in the corpus are skipped. Returns per-action status."
)
TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "matches": {
            "type": "array",
            "items": {"type": "object"},
            "description": "SourceMatch objects (from search_sources). Their "
                           "`ingest_action` is executed; matches without one are ignored.",
        },
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "tool": {"type": "string"},
                    "args": {"type": "object"},
                },
                "required": ["tool"],
            },
            "description": "Explicit ingest actions to execute.",
        },
        "skip_existing": {
            "type": "boolean",
            "default": True,
            "description": "Skip matches already present in the corpus (default true).",
        },
    },
}


async def _already_in_corpus(ingestion: Any, match: dict[str, Any]) -> bool:
    """Best-effort corpus-presence check for a match."""
    if match.get("availability") == "in_corpus" or match.get("document_id"):
        return True
    pattern = (match.get("metadata") or {}).get("corpus_source_pattern")
    if pattern and ingestion is not None and hasattr(ingestion, "find_existing"):
        try:
            existing = await ingestion.find_existing(source_pattern=pattern)
            return bool(existing)
        except Exception as exc:  # noqa: BLE001 — presence check is advisory
            logger.debug("ingest_execute_presence_check_failed", error=str(exc))
    return False


async def handler(
    container: Any,
    *,
    matches: list[dict[str, Any]] | None = None,
    actions: list[dict[str, Any]] | None = None,
    skip_existing: bool = True,
) -> dict[str, Any]:
    """Dispatch ingest actions, skipping matches already in the corpus."""
    from research_engine.mcp.dispatch import dispatch_tool

    ingestion = getattr(container, "ingestion", None)

    # Build the work list: explicit actions (no match context) + actions pulled
    # from match.ingest_action (carry the match for dedup).
    work: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
    for action in actions or []:
        work.append((action, None))
    for match in matches or []:
        ingest_action = match.get("ingest_action")
        if ingest_action and ingest_action.get("tool"):
            work.append((ingest_action, match))

    results: list[dict[str, Any]] = []
    for action, match in work:
        tool_id = action.get("tool")
        args = action.get("args") or {}

        if skip_existing and match is not None and await _already_in_corpus(ingestion, match):
            results.append({"tool": tool_id, "status": "skipped", "reason": "in_corpus"})
            continue

        if not tool_id:
            results.append({"tool": None, "status": "failed", "error": "action missing 'tool'"})
            continue

        try:
            result = await dispatch_tool(container, tool_id, args)
            failed = isinstance(result, dict) and result.get("error") is not None
            results.append({
                "tool": tool_id,
                "status": "failed" if failed else "ok",
                "result": result,
            })
        except Exception as exc:  # noqa: BLE001 — one bad action shouldn't abort the batch
            logger.error("ingest_execute_action_failed", tool=tool_id, error=str(exc))
            results.append({"tool": tool_id, "status": "failed", "error": str(exc)})

    summary = {"ok": 0, "skipped": 0, "failed": 0}
    for r in results:
        summary[r["status"]] = summary.get(r["status"], 0) + 1

    return {"results": results, "summary": summary}
