"""SDK types for plugin authors."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from research_engine.domain.common import DatePrecision


class SourceRef(BaseModel):
    """Reference to a source file or URI."""

    path: Path | None = None
    uri: str | None = None
    content_hash: bytes | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def ref(self) -> str:
        return str(self.path) if self.path else (self.uri or "")

    @property
    def is_local(self) -> bool:
        return self.path is not None


class DetectionResult(BaseModel):
    """Result of a module's detect() call."""

    confidence: float  # 0.0 to 1.0
    reason: str
    is_viable: bool = True


class ParsedDocument(BaseModel):
    """Result of parsing a source document."""

    title: str | None = None
    text: str
    document_type: str = "generic"
    language: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    sections: list[dict[str, Any]] = Field(default_factory=list)
    structural_locators: list[dict[str, Any]] = Field(default_factory=list)


class FuzzyDate(BaseModel):
    """A date with explicit precision."""

    start: datetime
    end: datetime
    precision: DatePrecision
