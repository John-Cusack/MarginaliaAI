"""Protocols implemented by plugin contributions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, Protocol, runtime_checkable

if TYPE_CHECKING:
    from pathlib import Path

    from research_engine_sdk.types import (
        DetectionResult,
        Document,
        ParsedDocument,
        PassageDraft,
        SourceMatch,
        SourceQuery,
    )


@runtime_checkable
class IngestionModule(Protocol):
    id: ClassVar[str]
    version: ClassVar[str]
    supported_extensions: ClassVar[list[str]]
    supported_mime_types: ClassVar[list[str]]

    async def detect(self, source_path: Path) -> DetectionResult | tuple[float, str]: ...

    async def parse(self, source_path: Path) -> ParsedDocument | tuple[str, str | None, dict[str, Any]]: ...

    def default_chunker(self) -> str: ...

    def default_document_type(self) -> str: ...

    def metadata_schema(self) -> dict[str, Any]: ...


@runtime_checkable
class Chunker(Protocol):
    id: ClassVar[str]
    version: ClassVar[str]
    consumes: ClassVar[str]
    max_passage_tokens: int | None

    async def chunk(
        self, text: str, metadata: dict[str, Any] | None = None
    ) -> list[PassageDraft]: ...


@runtime_checkable
class PostIngestionHook(Protocol):
    async def run(
        self, doc: Document, text: str, metadata: dict[str, Any]
    ) -> None: ...


@runtime_checkable
class SourceSearchProvider(Protocol):
    @property
    def plugin_name(self) -> str: ...

    async def search(self, query: SourceQuery, *, limit: int) -> list[SourceMatch]: ...
