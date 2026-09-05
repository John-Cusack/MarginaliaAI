"""work_render tool -- a work's body with footnotes derived from the corpus."""

from __future__ import annotations

from typing import Any

import structlog

from research_engine.services.works.files import WorkFileError

logger = structlog.get_logger()

TOOL_NAME = "work_render"
TOOL_DESCRIPTION = (
    "Render a work file's body with one footnote definition per citation, in id "
    "order. Author, title and year come from the corpus; the tier tag says how "
    "the quote verified; a footnote with any part missing is tagged "
    "[provisional]. Display strings only — nothing is written back."
)
TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "Work file relative to RE_WORKS_DIR.",
        },
    },
    "required": ["path"],
}


async def handler(container: Any, *, path: str) -> dict[str, Any]:
    renderer = getattr(container, "work_renderer", None)
    if renderer is None:
        return {
            "error": {
                "code": "works_not_configured",
                "message": "RE_WORKS_DIR is not set, so no work file can be read.",
                "details": None,
            }
        }
    try:
        reader = container.work_files
        if reader is not None and not (reader.works_dir / path).is_file():
            return {
                "error": {
                    "code": "not_found",
                    "message": f"Work file not found: {path}",
                    "details": None,
                }
            }
        return await renderer.render(path)
    except WorkFileError as exc:
        return {"error": {"code": "validation_error", "message": str(exc), "details": None}}
    except Exception as exc:  # noqa: BLE001 - the dispatch envelope for the unexpected
        logger.error("work_render_error", error=str(exc))
        return {"error": {"code": "work_render_failed", "message": str(exc), "details": None}}
