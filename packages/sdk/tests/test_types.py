from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

import pytest
from pydantic import ValidationError

from research_engine_sdk import DetectionResult, PassageDraft, PluginContext, SourceRef


def test_passage_draft_span_must_address_its_text() -> None:
    with pytest.raises(ValidationError, match="span width"):
        PassageDraft(
            position=0,
            char_start=10,
            char_end=15,
            text="four",
            chunker="custom",
            chunker_version="1",
        )


def test_source_ref_requires_a_location() -> None:
    with pytest.raises(ValidationError, match="path or uri"):
        SourceRef()


def test_boundary_dtos_validate_consumer_visible_fields(tmp_path: Path) -> None:
    assert DetectionResult(confidence=1.0, reason="exact").is_viable
    context = PluginContext(
        plugin_id="sample",
        data_dir=tmp_path,
        distribution_name="marginalia-ai-plugin-sample",
        distribution_version="1.2.3",
    )
    assert context.data_dir == tmp_path
    assert context.database_url is None

    with_database = PluginContext(
        **context.model_dump(exclude={"database_url"}),
        database_url="postgresql+asyncpg://user:secret@db/research",
    )
    assert with_database.database_url is not None
    assert with_database.database_url.get_secret_value().endswith("@db/research")
    assert "secret" not in repr(with_database)
