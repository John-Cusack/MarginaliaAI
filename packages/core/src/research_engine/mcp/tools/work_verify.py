"""work_verify tool -- check a work file's citations against the corpus."""

from __future__ import annotations

from typing import Any

import structlog

from research_engine.mcp.errors import envelope, failed
from research_engine.services.works.files import WorkFileError
from research_engine.services.works.verify import VerifyOutput

logger = structlog.get_logger()

TOOL_NAME = "work_verify"
TOOL_DESCRIPTION = (
    "Check a work file's front-matter citations against the corpus. Each entry "
    "is verified against the span it names (exact or normalized to pass), then "
    "checked for staleness, region narrowing, edition identity, edition key "
    "agreement, and body markers. Findings carry stable rule ids "
    "(AUTH_QUOTE_UNVERIFIED and friends); messages are for humans. "
    "Omit path to check every work. Gate review fails on any error; publish "
    "additionally fails on a missing edition."
)
TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": (
                "Work file relative to RE_WORKS_DIR, e.g. "
                "'deror-leviticus-25-translation.md'. Omit for all works."
            ),
        },
        "gate": {
            "type": "string",
            "enum": ["none", "review", "publish"],
            "default": "none",
            "description": "Which gate to judge the work against.",
        },
    },
}

_VALID_GATES = ("none", "review", "publish")


async def handler(
    container: Any,
    *,
    path: str | None = None,
    gate: str = "none",
) -> dict[str, Any]:
    verifier = getattr(container, "work_verifier", None)
    if verifier is None:
        return envelope("works_not_configured", "RE_WORKS_DIR is not set, so no work file can be read.", None)
    if gate not in _VALID_GATES:
        return envelope("validation_error", f"gate must be one of {_VALID_GATES}", None)
    try:
        if path is None:
            return (await verifier.verify_all(gate)).model_dump(mode="json")
        reader = container.work_files
        if reader is not None and not (reader.works_dir / path).is_file():
            return envelope("not_found", f"Work file not found: {path}", None)
        report = await verifier.verify_work(path, gate)  # type: ignore[arg-type]
        output = VerifyOutput(
            works=[report],
            summary={
                "works": 1,
                "citations": len(report.citations),
                "errors": sum(1 for f in report.findings if f.severity == "error"),
                "warnings": sum(1 for f in report.findings if f.severity == "warning"),
            },
        )
        return output.model_dump(mode="json")
    except WorkFileError as exc:
        return envelope("validation_error", str(exc), None)
    except Exception as exc:  # noqa: BLE001 - the dispatch envelope for the unexpected
        logger.error("work_verify_error", error=str(exc))
        return failed(TOOL_NAME, exc)
