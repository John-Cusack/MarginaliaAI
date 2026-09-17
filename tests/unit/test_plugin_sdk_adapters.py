from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from research_engine.adapters.event_client import EventServiceAdapter
from research_engine.adapters.ingestion_client import IngestionServiceAdapter
from research_engine.domain.events import Event as CoreEvent
from research_engine.domain.events import EventFilter as CoreEventFilter
from research_engine.domain.events import TimelineBucket as CoreTimelineBucket
from research_engine.plugins.loader import LoadedPlugin, PluginLoader
from research_engine.plugins.registry import PluginRegistry
from research_engine_sdk import (
    Event,
    EventClient,
    EventFilter,
    IngestionClient,
    PluginContext,
    PluginPermissions,
)


def test_ingestion_adapter_conforms_to_sdk_protocol() -> None:
    adapter = IngestionServiceAdapter(AsyncMock(), PluginRegistry())
    assert isinstance(adapter, IngestionClient)


async def test_full_text_ingest_uses_registered_chunker_and_canonical_text() -> None:
    registry = PluginRegistry()
    registry.register_document_type(
        "letter", {"default_chunker": "whole_or_paragraph"}, "history"
    )
    orchestrator = AsyncMock()
    orchestrator.ingest_drafts.return_value = {
        "document_id": "doc-1",
        "passage_count": 1,
        "node_count": 2,
    }
    adapter = IngestionServiceAdapter(orchestrator, registry)
    text = "# Letter\n\nDear reader."

    result = await adapter.ingest_document(
        title="Letter",
        document_type="letter",
        text=text,
        source="fixture:letter",
        sections=[
            {
                "char_start": 0,
                "char_end": len(text),
                "heading": "Letter",
                "level": 1,
            }
        ],
    )

    assert result["document_id"] == "doc-1"
    call = orchestrator.ingest_drafts.await_args
    assert call.kwargs["full_text"] == text
    assert call.args[2][0].text == text
    assert call.kwargs["node_drafts"][0].node_type == "document"
    assert call.kwargs["node_drafts"][1].title == "Letter"


def test_event_adapter_conforms_to_sdk_protocol() -> None:
    adapter = EventServiceAdapter(AsyncMock(), lambda: None)
    assert isinstance(adapter, EventClient)


async def test_event_query_accepts_and_returns_sdk_dtos() -> None:
    event = CoreEvent(
        id=uuid4(),
        event_type="letter_sent",
        timestamp_start=datetime(1863, 1, 1, tzinfo=UTC),
        created_at=datetime.now(UTC),
    )
    service = AsyncMock()
    service.query.return_value = (
        [event],
        [CoreTimelineBucket(bucket="1863-01", count=1)],
    )
    adapter = EventServiceAdapter(service, lambda: None)
    actor_id = uuid4()

    events, buckets = await adapter.query(
        EventFilter(event_types=["letter_sent"], actor_entity_ids=[actor_id]),
        group_by="month",
    )

    sent_filter = service.query.await_args.args[0]
    assert isinstance(sent_filter, CoreEventFilter)
    assert sent_filter.actor_entity_ids == [actor_id]
    assert isinstance(events[0], Event)
    assert buckets[0].bucket == "1863-01"


def test_loader_injects_distribution_context(tmp_path) -> None:
    registry = PluginRegistry()
    loader = PluginLoader(AsyncMock(), registry, tmp_path / "plugin-data")
    manifest = SimpleNamespace(permissions=PluginPermissions())
    discovery = SimpleNamespace(
        manifest=manifest,
        plugin_id="sample",
        distribution_name="marginalia-ai-plugin-sample",
        distribution_version="1.2.3",
    )
    loader._loaded["sample"] = LoadedPlugin(discovery)

    clients = loader.build_plugin_clients("sample")

    assert isinstance(clients["context"], PluginContext)
    assert clients["context"].plugin_id == "sample"
    assert clients["context"].distribution_version == "1.2.3"
    assert clients["context"].data_dir == (tmp_path / "plugin-data" / "sample").resolve()
    assert clients["context"].data_dir.is_dir()
