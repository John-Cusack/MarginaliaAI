"""Composition root — the single place where concrete adapters are wired together."""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any

from research_engine.adapters.clock import SystemClock
from research_engine.adapters.edge_client import EdgeServiceAdapter
from research_engine.adapters.extraction_client import ExtractionServiceAdapter
from research_engine.adapters.http.httpx_adapter import HttpxAdapter
from research_engine.adapters.inference import InferenceBackends, build_inference
from research_engine.adapters.llm.anthropic import AnthropicLLMAdapter
from research_engine.adapters.llm.budget_guard import BudgetGuard
from research_engine.adapters.llm.openai_compatible import OpenAICompatibleLLMAdapter
from research_engine.adapters.storage.postgres.engine import build_engine, transaction
from research_engine.adapters.storage.postgres.repositories import (
    PGDocumentNodeRepo,
    PGDocumentRepo,
    PGDocumentTextRepo,
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
from research_engine.services.extraction.postprocess import RecordEnricher
from research_engine.services.ingestion.dispatch import ModuleDispatcher
from research_engine.services.ingestion.orchestrator import IngestionOrchestrator
from research_engine.services.search.hybrid import HybridSearchService
from research_engine.services.search.windows import PassageWindowReader
from research_engine.services.verification import QuoteVerifier

if TYPE_CHECKING:
    from research_engine.config.settings import Settings


@dataclass
class Container:
    settings: Settings
    llm: Any
    #: The *bulk* embedder — CLI backfill and reindex use this, and both
    #: should fail rather than silently move a corpus-wide run onto a laptop.
    #: Search holds `inference.query_embedding` instead.
    embedding: Any
    reranker: Any
    inference: InferenceBackends
    http: Any
    clock: Any
    docs: PGDocumentRepo
    document_texts: PGDocumentTextRepo
    document_nodes: PGDocumentNodeRepo
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
    verification: QuoteVerifier
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
    def llm_calls_repo(self) -> PGLLMCallLogRepo:
        return self.llm_calls

    @property
    def transaction_factory(self) -> Any:
        """Open a transactional connection.

        Returns a zero-arg async context manager factory yielding a
        ``Transaction``. Used by write-path MCP tools (``upsert_edge``,
        ``upsert_entity``, ``upsert_event``).
        """
        return partial(transaction, self.engine)

    async def close(self) -> None:
        await self.http.close()
        await self.inference.close()
        await self.engine.dispose()


async def build_container(settings: Settings) -> Container:
    """Build and wire all dependencies."""
    sql_engine = await build_engine(settings.db_url)

    # Repositories
    docs = PGDocumentRepo(sql_engine)
    document_texts_repo = PGDocumentTextRepo(sql_engine)
    document_nodes_repo = PGDocumentNodeRepo(sql_engine)
    passages_repo = PGPassageRepo(sql_engine, ef_search=settings.hnsw_ef_search)
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

    http = HttpxAdapter()
    clock = SystemClock()

    # Wrap the LLM adapter before anything else takes a reference, so that every
    # caller — core services and plugin clients alike — is guarded.
    if settings.llm_budget_usd is not None:
        llm = BudgetGuard(
            llm,
            llm_calls_repo,
            clock,
            limit_usd=settings.llm_budget_usd,
            window_days=settings.llm_budget_window_days,
        )

    # Embedding and reranking are placed by `adapters/inference/routing.py`,
    # which also decides what an unreachable GPU host means. The query and bulk
    # embedders may be different objects with the same model identity: a query
    # can fall back to this machine, a corpus-wide run must not.
    inference = build_inference(settings)
    embedding = inference.bulk_embedding
    reranker = inference.reranker

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
        transaction_factory=partial(transaction, sql_engine),
        default_model=settings.default_llm_model,
        enricher=RecordEnricher(
            documents=docs,
            entities=entities_repo,
            document_nodes=document_nodes_repo,
        ),
    )

    quote_verifier = QuoteVerifier(
        document_texts=document_texts_repo,
        passages=passages_repo,
        documents=docs,
    )

    # What a hit is *read* as, rather than what it was ranked as. Always on:
    # a chunk boundary is where the ingester happened to cut, and there is no
    # query for which that is the right thing to hand a reader.
    window_reader = PassageWindowReader(
        document_nodes=document_nodes_repo,
        document_texts=document_texts_repo,
        max_tokens=settings.search_window_max_tokens,
        min_tokens=settings.search_window_min_tokens,
    )

    search_service = HybridSearchService(
        passages=passages_repo,
        embedding=inference.query_embedding,
        reranker=reranker,
        get_filter_extensions=registry.get_filter_extensions,
        windows=window_reader,
    )

    # Dispatcher with built-in modules
    dispatcher = ModuleDispatcher()
    _register_builtin_modules(dispatcher, settings)

    ingestion_service = IngestionOrchestrator(
        docs=docs,
        passages=passages_repo,
        embedding=embedding,
        ingestion_runs=ingestion_runs_repo,
        dispatcher=dispatcher,
        engine=sql_engine,
        concurrency=settings.ingest_concurrency,
        embedding_batch_size=settings.embedding_batch_size,
        default_language=settings.default_language,
        document_texts=document_texts_repo,
        document_nodes=document_nodes_repo,
    )

    # Plugin-facing client adapters. Built here (not in the Container) because
    # the loader needs them before the Container is constructed. Both reference
    # sql_engine directly via the same transaction factory the Container exposes.
    tx_factory = partial(transaction, sql_engine)
    edge_service = EdgeServiceAdapter(edges_repo, tx_factory)
    extraction_client = ExtractionServiceAdapter(
        extraction_service, passages_repo, extractions_repo
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
        document_nodes=document_nodes_repo,
        entity_service=entity_service,
        event_service=event_service,
        extraction=extraction_client,
        edge=edge_service,
        ingestion=ingestion_service,
    )
    await plugin_loader.load_enabled()

    return Container(
        settings=settings,
        llm=llm,
        embedding=embedding,
        reranker=reranker,
        inference=inference,
        http=http,
        clock=clock,
        docs=docs,
        document_texts=document_texts_repo,
        document_nodes=document_nodes_repo,
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
        verification=quote_verifier,
        extraction=extraction_service,
        entity_service=entity_service,
        event_service=event_service,
        plugin_loader=plugin_loader,
        plugin_registry=registry,
        engine=sql_engine,
    )


def _register_builtin_modules(
    dispatcher: ModuleDispatcher, settings: Settings
) -> None:
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
    #
    # It is the only module that needs configuring, and until now it was the only
    # component `build_container` did not configure: it read the environment
    # directly and sized its process pool from constants no operator could reach.
    dispatcher.register(
        DoclingModule(
            device=settings.docling_device,
            max_workers=settings.docling_max_workers,
            pages_per_task=settings.docling_pages_per_task,
        )
    )
    for mod_cls in [PlainTextModule, MarkdownModule, PDFTextModule, EPUBModule, HTMLModule, TEIXMLModule]:
        dispatcher.register(mod_cls())
