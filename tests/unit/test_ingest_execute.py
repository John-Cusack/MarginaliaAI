"""Unit tests for the ingest_execute orchestration tool."""

from __future__ import annotations

from research_engine.mcp.tools import ingest_execute


class _FakeIngestion:
    def __init__(self, present_patterns: set[str]):
        self._present = present_patterns

    async def find_existing(self, *, source=None, source_pattern=None):
        return [{"document_id": "doc-1"}] if source_pattern in self._present else []


class _FakeRegistry:
    """Minimal registry exposing one fake plugin tool."""

    def __init__(self, tools):
        self._tools = tools

    def get_mcp_tools(self):
        return self._tools

    def get_tool_plugin(self, tool_id):
        return "fake" if tool_id in self._tools else None


class _FakeLoader:
    def build_plugin_clients(self, plugin_name):
        return {}


class _FakeContainer:
    def __init__(self, ingestion, registry=None, loader=None):
        self.ingestion = ingestion
        self.registry = registry
        self.plugin_registry = registry
        self.plugin_loader = loader


async def test_skips_in_corpus_by_availability():
    container = _FakeContainer(_FakeIngestion(set()))
    res = await ingest_execute.handler(
        container,
        matches=[{"availability": "in_corpus",
                  "ingest_action": {"tool": "whatever", "args": {}}}],
    )
    assert res["summary"]["skipped"] == 1
    assert res["results"][0]["reason"] == "in_corpus"


async def test_skips_when_find_existing_hits():
    container = _FakeContainer(_FakeIngestion({"present.pat"}))
    res = await ingest_execute.handler(
        container,
        matches=[{"metadata": {"corpus_source_pattern": "present.pat"},
                  "ingest_action": {"tool": "whatever", "args": {}}}],
    )
    assert res["summary"]["skipped"] == 1


async def test_dispatches_to_plugin_tool_and_reports_ok():
    calls = {}

    async def fake_tool(**kwargs):
        calls.update(kwargs)
        return {"document_id": "new-doc"}

    registry = _FakeRegistry({"books_ingest_ia": fake_tool})
    container = _FakeContainer(_FakeIngestion(set()), registry, _FakeLoader())

    res = await ingest_execute.handler(
        container,
        matches=[{"metadata": {"corpus_source_pattern": "absent.pat"},
                  "availability": "ingestable",
                  "ingest_action": {"tool": "books_ingest_ia", "args": {"identifier": "abc"}}}],
    )
    assert res["summary"]["ok"] == 1
    assert calls == {"identifier": "abc"}
    assert res["results"][0]["result"] == {"document_id": "new-doc"}


async def test_unknown_tool_fails_without_aborting_batch():
    async def ok_tool(**kwargs):
        return {"ok": True}

    registry = _FakeRegistry({"good": ok_tool})
    container = _FakeContainer(_FakeIngestion(set()), registry, _FakeLoader())

    res = await ingest_execute.handler(
        container,
        actions=[{"tool": "nonexistent"}, {"tool": "good"}],
    )
    statuses = {r["tool"]: r["status"] for r in res["results"]}
    assert statuses == {"nonexistent": "failed", "good": "ok"}


async def test_result_with_error_marked_failed():
    async def err_tool(**kwargs):
        return {"error": {"code": "x", "message": "boom"}}

    registry = _FakeRegistry({"err": err_tool})
    container = _FakeContainer(_FakeIngestion(set()), registry, _FakeLoader())

    res = await ingest_execute.handler(container, actions=[{"tool": "err"}])
    assert res["results"][0]["status"] == "failed"
