"""The one error shape the agent ever sees."""
from __future__ import annotations

from typing import Any


def envelope(code: str, message: str, details: Any = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details}}


def failed(tool_name: str, exc: Exception, details: Any = None) -> dict[str, Any]:
    """A tool's catch-all. The code is derived, so it cannot drift from the name."""
    return envelope(f"{tool_name}_failed", str(exc), details)
