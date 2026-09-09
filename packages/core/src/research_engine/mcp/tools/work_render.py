"""work_render tool -- a work's body with footnotes derived from the corpus."""

from __future__ import annotations

from typing import Any

import structlog

from research_engine.mcp.errors import envelope, failed
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
        return envelope("works_not_configured", "RE_WORKS_DIR is not set, so no work file can be read.", None)
    try:
        reader = container.work_files
        if reader is not None and not (reader.works_dir / path).is_file():
            return envelope("not_found", f"Work file not found: {path}", None)
        return await renderer.render(path)
    except WorkFileError as exc:
        return envelope("validation_error", str(exc), None)
    except Exception as exc:  # noqa: BLE001 - the dispatch envelope for the unexpected
        logger.error("work_render_error", error=str(exc))
        return failed(TOOL_NAME, exc)
