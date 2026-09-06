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
    PGCitationRepo,
    PGDocumentNodeRepo,
    PGDocumentRepo,
    PGDocumentTextRepo,
    PGEdgeRepo,
    PGEditionRepo,
    PGEntityRepo,
    PGEventRepo,
    PGExtractionRepo,
    PGExtractionSchemaRepo,
    PGIngestionRunRepo,
    PGInstalledPluginRepo,
    PGLLMCallLogRepo,
    PGMentionRepo,
    PGPassageRepo,
    PGSourceSpanRepo,
    PGWaiverRepo,
    PGWorkBlockRepo,
    PGWorkLinkRepo,
    PGWorkRepo,
    PGWorkRevisionRepo,
)
from research_engine.plugins.loader import PluginLoader
from research_engine.plugins.registry import PluginRegistry
from research_engine.services.entities.service import EntityService
from research_engine.services.events.service import EventService
from research_engine.services.extraction.executor import ExtractionExecutor
from research_engine.services.extraction.postprocess import RecordEnricher
from research_engine.services.ingestion.dispatch import ModuleDispatcher
from research_engine.services.ingestion.orchestrator import IngestionOrchestrator
from research_engine.services.search.hit_source import HitSourceReader
from research_engine.services.search.hybrid import HybridSearchService
from research_engine.services.search.windows import PassageWindowReader
from research_engine.services.verification import QuoteVerifier
from research_engine.services.works.attach import CitationService
from research_engine.services.works.cite import WorkCiter
from research_engine.services.works.drafting import WorkExportService
from research_engine.services.works.files import WorkFileReader
from research_engine.services.works.publication import WorkPublicationService
from research_engine.services.works.render import WorkRenderer
from research_engine.services.works.trace import WorkTraceService
from research_engine.services.works.validate import WorkValidationService
from research_engine.services.works.verify import WorkVerifier
from research_engine.services.works.work_service import WorkService

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
    #: Work-file services. None when `RE_WORKS_DIR` is unset — tools finding
    #: them None answer `works_not_configured` rather than an empty result.
    work_files: WorkFileReader | None = None
    work_verifier: WorkVerifier | None = None
    work_renderer: WorkRenderer | None = None
    #: Citation making. Built always: citing needs the corpus, not the works
    #: directory — the entry is pasted by hand, not written to any file.
    work_citer: WorkCiter | None = None
    #: The Phase-1 spine: works as rows. Built always — rows live in the
    #: database, so no works directory is needed to draft, cite, or freeze.
    work_service: WorkService | None = None
    citation_service: CitationService | None = None
    work_validation: WorkValidationService | None = None
    work_publication: WorkPublicationService | None = None
    work_trace: WorkTraceService | None = None
    work_export: WorkExportService | None = None
    #: True once the Step 4 mirror (`core.works_index`) exists and
    #: `work_citations` should query it instead of scanning files.
    works_mirror_available: bool = False

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
    spans_repo = PGSourceSpanRepo(sql_engine)
    work_citer = WorkCiter(
        verification=quote_verifier,
        spans=spans_repo,
        engine=sql_engine,
    )

    # The Phase-1 spine: one repo per table group, services over them.
    tx_factory = partial(transaction, sql_engine)
    works_repo = PGWorkRepo(sql_engine)
    revisions_repo = PGWorkRevisionRepo(sql_engine)
    blocks_repo = PGWorkBlockRepo(sql_engine)
    citations_repo = PGCitationRepo(sql_engine)
    links_repo = PGWorkLinkRepo(sql_engine)
    editions_repo = PGEditionRepo(sql_engine)
    waivers_repo = PGWaiverRepo(sql_engine)
    work_service = WorkService(
        works=works_repo,
        revisions=revisions_repo,
        blocks=blocks_repo,
        citations=citations_repo,
        links=links_repo,
        spans=spans_repo,
        transaction_factory=tx_factory,
    )
    citation_service = CitationService(
        verification=quote_verifier,
        spans=spans_repo,
        editions=editions_repo,
        citations=citations_repo,
        works=works_repo,
        revisions=revisions_repo,
        blocks=blocks_repo,
        passages=passages_repo,
        documents=docs,
        transaction_factory=tx_factory,
    )
    work_export = WorkExportService(
        works=works_repo,
        revisions=revisions_repo,
        blocks=blocks_repo,
        citations=citations_repo,
        links=links_repo,
        spans=spans_repo,
        transaction_factory=tx_factory,
    )
    work_validation = WorkValidationService(
        works=works_repo,
        revisions=revisions_repo,
        blocks=blocks_repo,
        citations=citations_repo,
        links=links_repo,
        editions=editions_repo,
        waivers=waivers_repo,
        spans=spans_repo,
        documents=docs,
        document_texts=document_texts_repo,
        passages=passages_repo,
        policy=settings.works_policy,
        works_dir=settings.works_dir,
        export_markdown=work_export.export_draft_text,
    )
    work_publication = WorkPublicationService(
        validation=work_validation,
        works=works_repo,
        revisions=revisions_repo,
        blocks=blocks_repo,
        citations=citations_repo,
        links=links_repo,
        spans=spans_repo,
        waivers=waivers_repo,
        transaction_factory=tx_factory,
    )
    work_trace = WorkTraceService(
        works=works_repo,
        revisions=revisions_repo,
        blocks=blocks_repo,
        citations=citations_repo,
        links=links_repo,
        spans=spans_repo,
        documents=docs,
    )

    # Created works live as files until their first freeze. Without a works
    # directory there is nothing to verify, cite, or render, and the tools
    # say so instead of answering empty.
    if settings.works_dir is not None:
        work_files = WorkFileReader(settings.works_dir)
        work_verifier = WorkVerifier(
            document_texts_repo, docs, passages_repo, quote_verifier,
            settings.works_dir,
        )
        work_renderer = WorkRenderer(docs, quote_verifier, settings.works_dir)
    else:
        work_files = work_verifier = work_renderer = None

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
        hit_sources=HitSourceReader(
            documents=docs, document_texts=document_texts_repo
        ),
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
        editions=editions_repo,
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
        work_files=work_files,
        work_verifier=work_verifier,
        work_renderer=work_renderer,
        work_citer=work_citer,
        work_service=work_service,
        citation_service=citation_service,
        work_validation=work_validation,
        work_publication=work_publication,
        work_trace=work_trace,
        work_export=work_export,
        # The Step 4 mirror does not exist in Phase 0: no migration in this
        # change, so there is no table to detect. `work_citations` scans files.
        works_mirror_available=False,
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
