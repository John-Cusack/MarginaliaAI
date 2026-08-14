"""Base classes that plugins subclass."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from pathlib import Path

    from research_engine.domain.documents import Document
    from research_engine.domain.passages import PassageDraft


class IngestionModule(ABC):
    """Parses a source into a ParsedDocument. Does not chunk or embed."""

    id: ClassVar[str]
    version: ClassVar[str]
    supported_extensions: ClassVar[list[str]] = []
    supported_mime_types: ClassVar[list[str]] = []

    @abstractmethod
    async def detect(self, source_path: Path) -> tuple[float, str]:
        """Returns (confidence, reason) for whether this module handles source."""

    @abstractmethod
    async def parse(self, source_path: Path) -> tuple[str, str | None, dict[str, Any]]:
        """Parse source. Returns (full_text, title, metadata)."""

    def default_chunker(self) -> str:
        return "prose_window"

    def default_document_type(self) -> str:
        return "generic"

    def metadata_schema(self) -> dict:
        return {}


class Chunker(ABC):
    """Splits a parsed document into passage drafts."""

    id: ClassVar[str]
    version: ClassVar[str]

    @abstractmethod
    async def chunk(self, text: str, metadata: dict | None = None) -> list[PassageDraft]:
        ...


class PostIngestionHook(ABC):
    """Hook called after a document is fully ingested."""

    @abstractmethod
    async def run(self, doc: Document, text: str, metadata: dict[str, Any]) -> None:
        ...
