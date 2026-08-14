"""Tests for DoclingModule detection, chunker, and document type."""

from __future__ import annotations

from pathlib import PurePosixPath

import pytest

from research_engine.modules.docling_converter import DoclingModule


@pytest.fixture
def module() -> DoclingModule:
    return DoclingModule()


class TestDetect:
    """Detection confidence for various file extensions."""

    @pytest.mark.parametrize("ext", [".pdf", ".docx", ".pptx", ".xlsx"])
    async def test_high_confidence_formats(self, module: DoclingModule, ext: str) -> None:
        path = PurePosixPath(f"/tmp/test{ext}")
        confidence, reason = await module.detect(path)
        assert confidence == 0.95
        assert ext in reason

    @pytest.mark.parametrize("ext", [".html", ".htm", ".md", ".csv", ".png", ".jpg", ".tiff", ".tex"])
    async def test_medium_confidence_formats(self, module: DoclingModule, ext: str) -> None:
        path = PurePosixPath(f"/tmp/test{ext}")
        confidence, reason = await module.detect(path)
        assert confidence == 0.85
        assert ext in reason

    @pytest.mark.parametrize("ext", [".txt", ".epub", ".xml", ".json", ".odt", ".rtf"])
    async def test_unsupported_formats(self, module: DoclingModule, ext: str) -> None:
        path = PurePosixPath(f"/tmp/test{ext}")
        confidence, _reason = await module.detect(path)
        assert confidence == 0.0


class TestDefaults:
    """Default chunker and document type."""

    def test_default_chunker(self, module: DoclingModule) -> None:
        assert module.default_chunker() == "structural"

    def test_default_document_type(self, module: DoclingModule) -> None:
        assert module.default_document_type() == "generic"

    @pytest.mark.parametrize(
        ("ext", "expected"),
        [
            (".xlsx", "spreadsheet"),
            (".csv", "spreadsheet"),
            (".pptx", "presentation"),
            (".pdf", "generic"),
            (".docx", "generic"),
            (".html", "generic"),
        ],
    )
    def test_document_type_for_extension(self, module: DoclingModule, ext: str, expected: str) -> None:
        path = PurePosixPath(f"/tmp/file{ext}")
        assert module.default_document_type_for(path) == expected


class TestModuleAttributes:
    def test_id(self, module: DoclingModule) -> None:
        assert module.id == "docling"

    def test_version(self, module: DoclingModule) -> None:
        assert module.version == "1.0"
