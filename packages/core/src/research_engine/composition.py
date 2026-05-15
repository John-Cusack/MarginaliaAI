"""Composition root — the single place where concrete adapters are wired together."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from research_engine.adapters.clock import SystemClock
from research_engine.adapters.embedding.local_bge import LocalBGEEmbedding
from research_engine.adapters.http.httpx_adapter import HttpxAdapter
from research_engine.adapters.llm.anthropic import AnthropicLLMAdapter
from research_engine.adapters.llm.openai_compatible import OpenAICompatibleLLMAdapter
from research_engine.adapters.reranker.local_bge import LocalBGEReranker
from research_engine.adapters.reranker.noop import NoopReranker
from research_engine.adapters.storage.postgres.engine import build_engine
from research_engine.adapters.storage.postgres.repositories import (
    PGDocumentRepo,
    PGEdgeRepo,
    PGEntityRepo,
    PGEventRepo,
    PGExtractionRepo,
    PGExtractionSchemaRepo,
    PGIngestionRunRepo,
    PGInstalledPluginRepo,
    PGLLMCallLogRepo,
    PGMentionRepo,
    PGPassageRepo,
)
from research_engine.plugins.loader import PluginLoader
from research_engine.plugins.registry import PluginRegistry
from research_engine.services.entities.service import EntityService
from research_engine.services.events.service import EventService
from research_engine.services.extraction.executor import ExtractionExecutor
from research_engine.services.ingestion.dispatch import ModuleDispatcher
from research_engine.services.ingestion.orchestrator import IngestionOrchestrator
from research_engine.services.search.hybrid import HybridSearchService

if TYPE_CHECKING:
    from research_engine.config.settings import Settings


@dataclass
class Container:
    settings: Settings
    llm: Any
    embedding: Any
    reranker: Any
    http: Any
    clock: Any
    docs: PGDocumentRepo
    passages: PGPassageRepo
    entities: PGEntityRepo
    mentions: PGMentionRepo
    events: PGEventRepo
    edges: PGEdgeRepo
    extractions: PGExtractionRepo
    extraction_schemas: PGExtractionSchemaRepo
    llm_calls: PGLLMCallLogRepo
    ingestion_runs: PGIngestionRunRepo
    installed_plugins: PGInstalledPluginRepo
    ingestion: IngestionOrchestrator
    search: HybridSearchService
    extraction: ExtractionExecutor
    entity_service: EntityService
    event_service: EventService
    plugin_loader: PluginLoader
    plugin_registry: PluginRegistry
    engine: Any  # AsyncEngine

    # Aliases used by MCP tool handlers in research_engine.mcp.tools.*
    @property
    def registry(self) -> PluginRegistry:
        return self.plugin_registry

    @property
    def search_service(self) -> HybridSearchService:
        return self.search

    @property
    def document_repo(self) -> PGDocumentRepo:
        return self.docs

    @property
    def passage_repo(self) -> PGPassageRepo:
        return self.passages

    @property
    def entity_repo(self) -> PGEntityRepo:
        return self.entities

    @property
    def mention_repo(self) -> PGMentionRepo:
        return self.mentions

    @property
    def event_repo(self) -> PGEventRepo:
        return self.events

    @property
    def edge_repo(self) -> PGEdgeRepo:
        return self.edges

    @property
    def extraction_repo(self) -> PGExtractionRepo:
        return self.extractions

    @property
    def extraction_schema_repo(self) -> PGExtractionSchemaRepo:
        return self.extraction_schemas

    @property
    def extraction_executor(self) -> ExtractionExecutor:
        return self.extraction

    @property
    def transaction_factory(self) -> Any:
        """Transaction factory — not yet wired, placeholder for MCP tool compat."""
        return None

    async def close(self) -> None:
        await self.http.close()
        await self.engine.dispose()


async def build_container(settings: Settings) -> Container:
    """Build and wire all dependencies."""
    sql_engine = await build_engine(settings.db_url)

    # Repositories
    docs = PGDocumentRepo(sql_engine)
    passages_repo = PGPassageRepo(sql_engine)
    entities_repo = PGEntityRepo(sql_engine)
    mentions_repo = PGMentionRepo(sql_engine)
    events_repo = PGEventRepo(sql_engine)
    edges_repo = PGEdgeRepo(sql_engine)
    extractions_repo = PGExtractionRepo(sql_engine)
    extraction_schemas_repo = PGExtractionSchemaRepo(sql_engine)
    llm_calls_repo = PGLLMCallLogRepo(sql_engine)
    ingestion_runs_repo = PGIngestionRunRepo(sql_engine)
    installed_plugins_repo = PGInstalledPluginRepo(sql_engine)

    # External ports
    if settings.llm_provider == "anthropic":
        llm = AnthropicLLMAdapter(
            settings.anthropic_api_key,
            llm_calls_repo,
            settings.default_llm_model,
        )
    else:
        llm = OpenAICompatibleLLMAdapter(
            settings.openai_compatible_base_url or "http://localhost:8000/v1",
            settings.openai_compatible_api_key.get_secret_value() if settings.openai_compatible_api_key else None,
            llm_calls_repo,
            settings.default_llm_model,
        )

    embedding = LocalBGEEmbedding(settings.embedding_model, settings.embedding_dim)

    if settings.reranker_provider == "local_bge":
        reranker = LocalBGEReranker(settings.reranker_model)
    else:
        reranker = NoopReranker()

    http = HttpxAdapter()
    clock = SystemClock()

    # Plugin registry
    registry = PluginRegistry()
    registry.register_core_types()

    # Built-in filter extensions
    from research_engine.services.search.filter_extensions import (
        EventDateRangeFilter,
        HasExtractionFilter,
    )

    _event_filter = EventDateRangeFilter()
    _extraction_filter = HasExtractionFilter()
    registry.register_filter_extension(_event_filter.filter_id, _event_filter, "core")
    registry.register_filter_extension(_extraction_filter.filter_id, _extraction_filter, "core")

    # Set global reference for pipeline.py to resolve plugin chunkers
    from research_engine.plugins import registry as _reg_mod

    _reg_mod._global_registry = registry

    # Services
    entity_service = EntityService(entities_repo, mentions_repo)
    event_service = EventService(events_repo)

    extraction_service = ExtractionExecutor(
        llm=llm,
        passages=passages_repo,
        extractions=extractions_repo,
        extraction_schemas=extraction_schemas_repo,
        default_model=settings.default_llm_model,
    )

    search_service = HybridSearchService(
        passages=passages_repo,
        embedding=embedding,
        reranker=reranker,
        get_filter_extensions=registry.get_filter_extensions,
    )

    # Dispatcher with built-in modules
    dispatcher = ModuleDispatcher()
    _register_builtin_modules(dispatcher)

    ingestion_service = IngestionOrchestrator(
        docs=docs,
        passages=passages_repo,
        embedding=embedding,
        ingestion_runs=ingestion_runs_repo,
        dispatcher=dispatcher,
        engine=sql_engine,
        concurrency=settings.ingest_concurrency,
        embedding_batch_size=settings.embedding_batch_size,
    )

    # Plugin loader
    plugin_loader = PluginLoader(
        installed_plugins=installed_plugins_repo,
        registry=registry,
        plugins_dir=settings.resolved_plugins_dir,
        llm=llm,
        http=http,
        search=search_service,
        documents=docs,
        passages=passages_repo,
        entity_service=entity_service,
        event_service=event_service,
        extraction=extraction_service,
        ingestion=ingestion_service,
    )
    await plugin_loader.load_enabled()

    return Container(
        settings=settings,
        llm=llm,
        embedding=embedding,
        reranker=reranker,
        http=http,
        clock=clock,
        docs=docs,
        passages=passages_repo,
        entities=entities_repo,
        mentions=mentions_repo,
        events=events_repo,
        edges=edges_repo,
        extractions=extractions_repo,
        extraction_schemas=extraction_schemas_repo,
        llm_calls=llm_calls_repo,
        ingestion_runs=ingestion_runs_repo,
        installed_plugins=installed_plugins_repo,
        ingestion=ingestion_service,
        search=search_service,
        extraction=extraction_service,
        entity_service=entity_service,
        event_service=event_service,
        plugin_loader=plugin_loader,
        plugin_registry=registry,
        engine=sql_engine,
    )


def _register_builtin_modules(dispatcher: ModuleDispatcher) -> None:
    """Register core ingestion modules."""
    from research_engine.modules.docling_converter import DoclingModule
    from research_engine.modules.epub import EPUBModule
    from research_engine.modules.html import HTMLModule
    from research_engine.modules.markdown import MarkdownModule
    from research_engine.modules.pdf_text import PDFTextModule
    from research_engine.modules.plain_text import PlainTextModule
    from research_engine.modules.tei_xml import TEIXMLModule

    # DoclingModule first — highest confidence for supported formats.
    # Existing modules remain as fallbacks.
    for mod_cls in [DoclingModule, PlainTextModule, MarkdownModule, PDFTextModule, EPUBModule, HTMLModule, TEIXMLModule]:
        dispatcher.register(mod_cls())
