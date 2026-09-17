from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path


from research_engine.composition import _register_builtin_modules
from research_engine.config.settings import Settings
from research_engine.domain.errors import ConfigurationError
from research_engine.modules.unavailable_document_ai import DocumentAIUnavailableModule


class Dispatcher:
    def __init__(self) -> None:
        self.modules = []

    def register(self, module) -> None:
        self.modules.append(module)


async def test_docling_absence_registers_actionable_module(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("research_engine.composition.find_spec", lambda name: None)
    dispatcher = Dispatcher()
    settings = SimpleNamespace(
        docling_device="cpu",
        docling_max_workers=1,
        docling_pages_per_task=10,
    )

    _register_builtin_modules(dispatcher, settings)

    ids = {module.id for module in dispatcher.modules}
    assert "docling" not in ids
    assert "document_ai_unavailable" in ids
    assert "pdf_text" in ids
    unavailable = next(
        module
        for module in dispatcher.modules
        if isinstance(module, DocumentAIUnavailableModule)
    )
    with pytest.raises(ConfigurationError, match=r"research-engine\[document-ai\]"):
        await unavailable.parse(tmp_path / "paper.docx")


def test_remote_inference_does_not_import_local_adapters() -> None:
    local_modules = {
        "research_engine.adapters.embedding.local_bge",
        "research_engine.adapters.reranker.local_bge",
    }
    for module in local_modules:
        sys.modules.pop(module, None)

    from research_engine.adapters.inference.routing import build_inference

    build_inference(
        Settings(
            embedding_provider="remote_api",
            reranker_provider="none",
            inference_base_url="http://gpu-host:9882",
        )
    )

    assert local_modules.isdisjoint(sys.modules)


def test_missing_local_inference_extra_is_actionable(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    sys.modules.pop("research_engine.adapters.embedding.local_bge", None)
    from research_engine.adapters.inference.routing import build_inference

    with pytest.raises(
        ConfigurationError,
        match=r"research-engine\[local-inference\]",
    ):
        build_inference(
            Settings(
                embedding_provider="local_bge",
                reranker_provider="none",
            )
        )
