"""What a pack can ask of ingestion beyond "store this".

A letter split out of its volume carries its own date at ingest, has that date
revised on review, and is deleted when its slice of the volume moves. Content
is never updatable: a document whose text changes is a different document.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from research_engine.adapters.ingestion_client import IngestionServiceAdapter
from research_engine.plugins.permissions import DeniedIngestionClient
from research_engine.plugins.registry import PluginRegistry
from research_engine_sdk import PermissionDenied


class RecordingOrchestrator:
    def __init__(self) -> None:
        self.kwargs: dict = {}

    async def ingest_drafts(self, title, document_type, drafts, **kwargs):
        self.kwargs = kwargs
        return {"document_id": "d", "passage_count": len(drafts)}


class RecordingDocuments:
    def __init__(self, exists: bool = True) -> None:
        self.exists = exists
        self.values: dict = {}
        self.patch: dict | None = None
        self.deleted: list = []

    async def update_fields(self, doc_id, values, metadata_patch=None):
        self.values, self.patch = values, metadata_patch
        if not self.exists:
            return None
        return SimpleNamespace(
            id=doc_id,
            title="t",
            document_type="letter",
            created_date_start=values.get("created_date_start"),
            created_date_end=values.get("created_date_end"),
            created_precision=values.get("created_precision"),
            metadata=metadata_patch or {},
        )

    async def get(self, doc_id):
        return SimpleNamespace(id=doc_id) if self.exists else None

    async def delete(self, doc_id):
        self.deleted.append(doc_id)


def a_registry() -> PluginRegistry:
    registry = PluginRegistry()
    registry.register_core_types()
    registry.register_document_type(
        "letter", {"default_chunker": "whole_or_paragraph"}, "history"
    )
    return registry


async def test_a_letter_is_ingested_with_its_own_date():
    orchestrator = RecordingOrchestrator()
    client = IngestionServiceAdapter(orchestrator, a_registry())
    edition = uuid4()

    await client.ingest_document(
        title="Faraday to his mother, Geneva, 1 July 1814",
        document_type="letter",
        text="FARADAY TO HIS MOTHER.\n\n' Geneva : July 1.",
        source="letter-collection:v#218864",
        created_date_start="1814-07-01T00:00:00+00:00",
        created_date_end="1814-07-01T23:59:59+00:00",
        created_precision="day",
        edition_id=str(edition),
    )

    assert orchestrator.kwargs["created_date_start"] == datetime(1814, 7, 1, tzinfo=UTC)
    assert orchestrator.kwargs["created_precision"] == "day"
    assert orchestrator.kwargs["edition_id"] == edition


async def test_an_update_sets_only_what_it_names():
    documents = RecordingDocuments()
    client = IngestionServiceAdapter(RecordingOrchestrator(), a_registry(), documents)

    result = await client.update_document(
        uuid4(), created_date_start="1815-02-13T00:00:00+00:00", metadata={"flags": []}
    )

    assert documents.values == {"created_date_start": datetime(1815, 2, 13, tzinfo=UTC)}
    assert documents.patch == {"flags": []}
    assert result["created_date_start"].startswith("1815-02-13")


async def test_clearing_a_date_clears_all_three_columns():
    documents = RecordingDocuments()
    client = IngestionServiceAdapter(RecordingOrchestrator(), a_registry(), documents)

    await client.update_document(uuid4(), clear_created_date=True)

    assert documents.values == {
        "created_date_start": None,
        "created_date_end": None,
        "created_precision": None,
    }


async def test_an_unknown_type_is_refused():
    client = IngestionServiceAdapter(
        RecordingOrchestrator(), a_registry(), RecordingDocuments()
    )
    with pytest.raises(Exception, match="no_such_type"):
        await client.update_document(uuid4(), document_type="no_such_type")


async def test_deleting_reports_whether_it_existed():
    present = RecordingDocuments(exists=True)
    absent = RecordingDocuments(exists=False)

    assert await IngestionServiceAdapter(
        RecordingOrchestrator(), a_registry(), present
    ).delete_document(uuid4()) is True
    assert await IngestionServiceAdapter(
        RecordingOrchestrator(), a_registry(), absent
    ).delete_document(uuid4()) is False
    assert absent.deleted == []


@pytest.mark.parametrize("method", ["update_document", "delete_document"])
async def test_without_ingest_permission_both_are_denied(method):
    with pytest.raises(PermissionDenied):
        await getattr(DeniedIngestionClient("history"), method)(uuid4())
