from __future__ import annotations

from research_engine_sdk import IngestionClient, hook, tool


class FakeIngestionClient:
    async def ingest_paths(self, paths, hint=None):
        return {"ok": len(paths)}

    async def ingest_document(self, **kwargs):
        return {"document_id": "doc", "passage_count": 1}

    async def ingest_drafts(
        self,
        title,
        document_type,
        passage_drafts,
        *,
        source="",
        metadata=None,
        language=None,
        full_text=None,
        node_drafts=None,
    ):
        return {"document_id": "doc", "passage_count": len(passage_drafts)}

    async def find_existing(self, *, source=None, source_pattern=None):
        return []


def test_protocol_accepts_structural_mock() -> None:
    assert isinstance(FakeIngestionClient(), IngestionClient)


def test_tool_decorator_preserves_behavior_and_metadata() -> None:
    @tool(
        id="sample.echo",
        description="Echo text",
        input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
    )
    async def echo(text: str) -> str:
        return text

    assert echo.__name__ == "echo"
    assert echo._tool_id == "sample.echo"  # type: ignore[attr-defined]
    assert echo._tool_input_schema["type"] == "object"  # type: ignore[attr-defined]


def test_hook_decorator_records_scope() -> None:
    @hook(document_types=["letter"])
    async def after_ingest() -> None:
        return None

    assert after_ingest._hook_event == "post_ingestion"  # type: ignore[attr-defined]
    assert after_ingest._hook_document_types == ["letter"]  # type: ignore[attr-defined]
