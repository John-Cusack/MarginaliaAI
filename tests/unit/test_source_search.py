"""Tests for the source-search plugin system: protocol, registry, fan-out, dedup."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from research_engine.domain.source_search import (
    Availability,
    IngestAction,
    SourceMatch,
    SourceQuery,
    SourceSearchProvider,
)
from research_engine.mcp.tools import search_sources
from research_engine.plugins.manifest import PluginManifest, parse_manifest
from research_engine.plugins.registry import PluginRegistry

# ---------- Fixtures ----------


class StubProvider:
    def __init__(self, name: str, matches: list[SourceMatch], delay: float = 0.0,
                 raises: BaseException | None = None) -> None:
        self.plugin_name = name
        self._matches = matches
        self._delay = delay
        self._raises = raises

    async def search(self, query: SourceQuery, *, limit: int) -> list[SourceMatch]:
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._raises:
            raise self._raises
        return list(self._matches[:limit])


def _match(plugin: str, title: str, *, doi: str | None = None,
           availability: Availability = Availability.ingestable,
           confidence: float = 0.5, year: int | None = 2020,
           authors: list[str] | None = None) -> SourceMatch:
    md: dict[str, Any] = {}
    if doi:
        md["doi"] = doi
    return SourceMatch(
        plugin=plugin,
        source_id=f"{plugin}-{title}",
        title=title,
        authors=authors or ["Smith, J."],
        year=year,
        availability=availability,
        confidence=confidence,
        ingest_action=IngestAction(tool=f"{plugin}_ingest", args={"id": title}),
        metadata=md,
    )


class StubContainer:
    """Just enough of Container to satisfy search_sources.handler."""

    def __init__(self, registry: PluginRegistry, ingestion: Any | None = None) -> None:
        self.plugin_registry = registry
        self.ingestion = ingestion

    @property
    def registry(self) -> PluginRegistry:
        return self.plugin_registry


# ---------- Protocol conformance ----------


class TestProtocol:
    def test_stub_is_provider(self) -> None:
        assert isinstance(StubProvider("acad", []), SourceSearchProvider)


# ---------- Registry ----------


class TestRegistry:
    def test_register_and_get(self) -> None:
        reg = PluginRegistry()
        provider = StubProvider("acad", [])
        reg.register_source_search_provider(provider, "academic-journal")
        assert reg.get_source_search_providers() == {"acad": provider}

    def test_conflict_across_plugins(self) -> None:
        from research_engine.domain.errors import PluginConflict
        reg = PluginRegistry()
        reg.register_source_search_provider(StubProvider("acad", []), "academic-journal")
        with pytest.raises(PluginConflict):
            reg.register_source_search_provider(StubProvider("acad", []), "imposter")


# ---------- Manifest ----------


class TestManifest:
    def test_source_search_field_default_empty(self) -> None:
        m = PluginManifest(name="x", version="0.0.1", author="t", description="t")
        assert m.provides.source_search == []

    def test_parses_source_search(self, tmp_path) -> None:
        p = tmp_path / "pack.yaml"
        p.write_text("""\
name: x
version: 0.0.1
author: t
description: t
provides:
  source_search:
    - id: foo
      entry: foo.mod:Provider
      description: stub
""")
        m = parse_manifest(p)
        assert len(m.provides.source_search) == 1
        assert m.provides.source_search[0].entry == "foo.mod:Provider"


# ---------- Fan-out tool ----------


class TestSearchSourcesTool:
    @pytest.mark.asyncio
    async def test_no_providers_returns_note(self) -> None:
        reg = PluginRegistry()
        container = StubContainer(reg)
        out = await search_sources.handler(container, query="anything")
        assert out["matches"] == []
        assert "no source search providers" in out["note"].lower()

    @pytest.mark.asyncio
    async def test_basic_fan_out(self) -> None:
        reg = PluginRegistry()
        reg.register_source_search_provider(
            StubProvider("acad", [_match("acad", "Foo")]), "p1"
        )
        reg.register_source_search_provider(
            StubProvider("ycl", [_match("ycl", "Bar")]), "p2"
        )
        out = await search_sources.handler(StubContainer(reg), query="x")
        titles = sorted(m["title"] for m in out["matches"])
        assert titles == ["Bar", "Foo"]
        assert set(out["providers_queried"]) == {"acad", "ycl"}

    @pytest.mark.asyncio
    async def test_provider_exception_isolated(self) -> None:
        """A broken provider must not poison the rest."""
        reg = PluginRegistry()
        reg.register_source_search_provider(
            StubProvider("acad", [_match("acad", "Foo")]), "p1"
        )
        reg.register_source_search_provider(
            StubProvider("broken", [], raises=RuntimeError("boom")), "p2"
        )
        out = await search_sources.handler(StubContainer(reg), query="x")
        assert len(out["matches"]) == 1
        assert out["matches"][0]["plugin"] == "acad"

    @pytest.mark.asyncio
    async def test_per_provider_timeout(self) -> None:
        reg = PluginRegistry()
        reg.register_source_search_provider(
            StubProvider("slow", [_match("slow", "Late")], delay=2.0), "p1"
        )
        reg.register_source_search_provider(
            StubProvider("fast", [_match("fast", "Quick")]), "p2"
        )
        out = await search_sources.handler(
            StubContainer(reg), query="x", timeout_s=0.05
        )
        plugins = [m["plugin"] for m in out["matches"]]
        assert plugins == ["fast"]

    @pytest.mark.asyncio
    async def test_dedup_by_doi_keeps_higher_availability(self) -> None:
        reg = PluginRegistry()
        # Same DOI, different availability — ingestable should win over external_only
        reg.register_source_search_provider(
            StubProvider("acad", [_match("acad", "Same Paper", doi="10.1/x",
                                         availability=Availability.external_only)]),
            "p1",
        )
        reg.register_source_search_provider(
            StubProvider("logos", [_match("logos", "Same Paper", doi="10.1/x",
                                          availability=Availability.ingestable)]),
            "p2",
        )
        out = await search_sources.handler(StubContainer(reg), query="x")
        assert out["raw_count"] == 2
        assert out["deduped_count"] == 1
        winner = out["matches"][0]
        assert winner["plugin"] == "logos"
        also = winner["metadata"]["also_available_via"]
        assert len(also) == 1
        assert also[0]["plugin"] == "acad"

    @pytest.mark.asyncio
    async def test_dedup_by_first_class_doi_field(self) -> None:
        # Providers set doi as a first-class SourceMatch field (not metadata);
        # dedup must still collapse them to one.
        reg = PluginRegistry()
        a = SourceMatch(plugin="acad", source_id="a", title="Same Paper",
                        doi="10.1/x", availability=Availability.external_only)
        b = SourceMatch(plugin="logos", source_id="b", title="Same Paper",
                        doi="10.1/x", availability=Availability.ingestable)
        reg.register_source_search_provider(StubProvider("acad", [a]), "p1")
        reg.register_source_search_provider(StubProvider("logos", [b]), "p2")
        out = await search_sources.handler(StubContainer(reg), query="x")
        assert out["raw_count"] == 2
        assert out["deduped_count"] == 1
        assert out["matches"][0]["plugin"] == "logos"

    @pytest.mark.asyncio
    async def test_filter_matching_no_provider_reports_available(self) -> None:
        reg = PluginRegistry()
        reg.register_source_search_provider(
            StubProvider("acad", [_match("acad", "A")]), "p1"
        )
        out = await search_sources.handler(
            StubContainer(reg), query="x", sources=["Acad"]  # wrong case
        )
        assert out["matches"] == []
        assert "acad" in out["note"]
        assert "registered" not in out["note"].lower() or "None of the requested" in out["note"]

    @pytest.mark.asyncio
    async def test_dedup_by_normalized_title_when_no_doi(self) -> None:
        reg = PluginRegistry()
        reg.register_source_search_provider(
            StubProvider("a", [_match("a", "The Lord of the Rings",
                                      authors=["Tolkien, J.R.R."])]),
            "p1",
        )
        reg.register_source_search_provider(
            StubProvider("b", [_match("b", "the lord of the rings!",
                                      authors=["Tolkien, J.R.R."])]),
            "p2",
        )
        out = await search_sources.handler(StubContainer(reg), query="x")
        assert out["deduped_count"] == 1

    @pytest.mark.asyncio
    async def test_sources_filter_restricts_providers(self) -> None:
        reg = PluginRegistry()
        reg.register_source_search_provider(
            StubProvider("acad", [_match("acad", "A")]), "p1"
        )
        reg.register_source_search_provider(
            StubProvider("ycl", [_match("ycl", "B")]), "p2"
        )
        out = await search_sources.handler(
            StubContainer(reg), query="x", sources=["ycl"]
        )
        assert out["providers_queried"] == ["ycl"]
        assert [m["plugin"] for m in out["matches"]] == ["ycl"]

    @pytest.mark.asyncio
    async def test_corpus_enrichment_sets_in_corpus(self) -> None:
        class StubIngestion:
            async def find_existing(self, *, source: str) -> list[dict]:
                # Exact match only — mirrors the orchestrator's source= path.
                if source == "doi.org/10.1/found":
                    return [{"document_id": "doc-uuid-1"}]
                return []

        reg = PluginRegistry()
        m = _match("acad", "Already Ingested", doi="10.1/found")
        m.metadata["corpus_source"] = "doi.org/10.1/found"
        reg.register_source_search_provider(StubProvider("acad", [m]), "p1")

        out = await search_sources.handler(
            StubContainer(reg, ingestion=StubIngestion()), query="x"
        )
        assert out["matches"][0]["availability"] == "in_corpus"
        assert out["matches"][0]["document_id"] == "doc-uuid-1"

    @pytest.mark.asyncio
    async def test_corpus_enrichment_uses_exact_match_no_false_positive(self) -> None:
        # A substring-style hint ('10.1/1') must NOT enrich when only an
        # unrelated source ('10.1/100') is ingested — exact match required.
        class StubIngestion:
            async def find_existing(self, *, source: str) -> list[dict]:
                if source == "10.1/100":
                    return [{"document_id": "other-doc"}]
                return []

        reg = PluginRegistry()
        m = _match("acad", "Different Paper", doi="10.1/1")
        m.metadata["corpus_source"] = "10.1/1"
        reg.register_source_search_provider(StubProvider("acad", [m]), "p1")

        out = await search_sources.handler(
            StubContainer(reg, ingestion=StubIngestion()), query="x"
        )
        assert out["matches"][0]["availability"] != "in_corpus"
        assert out["matches"][0]["document_id"] is None
