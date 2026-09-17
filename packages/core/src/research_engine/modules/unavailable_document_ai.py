"""Actionable ingestion response when the document-ai extra is absent."""

from __future__ import annotations

from typing import TYPE_CHECKING

from research_engine.domain.errors import ConfigurationError

if TYPE_CHECKING:
    from pathlib import Path


class DocumentAIUnavailableModule:
    """Claim formats whose only built-in parser is optional Docling."""

    id = "document_ai_unavailable"
    version = "1.0"
    supported_extensions = {
        ".docx",
        ".pptx",
        ".xlsx",
        ".png",
        ".jpg",
        ".jpeg",
        ".tiff",
        ".tif",
        ".bmp",
    }
    supported_mime_types: set[str] = set()

    async def detect(self, source_path: Path) -> tuple[float, str]:
        suffix = source_path.suffix.lower()
        if suffix in self.supported_extensions:
            return 0.94, f"extension {suffix!r} requires document-ai support"
        return 0.0, "format does not require document-ai support"

    async def parse(self, source_path: Path):
        raise ConfigurationError(
            f"Parsing {source_path.suffix.lower() or 'this format'} requires "
            "Docling. Install research-engine[document-ai]."
        )

    def default_chunker(self) -> str:
        return "structural"

    def default_document_type(self) -> str:
        return "generic"
