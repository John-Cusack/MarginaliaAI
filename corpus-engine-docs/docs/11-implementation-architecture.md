# Software Architecture & Implementation Guide

**Status:** Engineering handoff document. Read after docs 01–10.

**Audience:** Senior engineers implementing the system. Assumes comfort
with Python 3.11+, Postgres, async I/O, plugin architectures, and LLM
integration patterns.

**Purpose:** Turn the product-level specs into concrete implementation
guidance. Where earlier docs said *what* and *why*, this says *how* —
with real module boundaries, wiring patterns, and extension points. A
special focus on the plugin architecture, because that's what enables
third-party integrations (e.g., a Logos-style research-platform
connector) without coupling the core to any particular specialty.

---

## 1. Architectural stance

A few architectural choices deserve upfront statement because
everything else follows from them.

### 1.1 Modular monolith, not microservices

The system is one process with clean internal module boundaries, not a
cluster of services. This is non-negotiable for v1:

- Target deployment is a researcher's laptop. Microservices are
  operationally unfit for that context.
- Plugins must run cheaply; IPC overhead per tool call breaks the
  "compose many small calls" agent pattern the MCP surface encourages.
- All storage is a single Postgres. No distributed transactions, no
  eventual consistency, no cross-service auth.

Modular monolith means strict internal boundaries *enforced by module
conventions* (packages that don't import each other except through
declared APIs) and *validated by tests*, not enforced by process
boundaries.

### 1.2 Ports-and-adapters at the boundary

The core engine depends only on abstract ports for everything that
crosses a system boundary:

- LLM calls → `LLMPort`
- Embedding → `EmbeddingPort`
- Reranker → `RerankerPort`
- Storage → repository interfaces
- Network fetches (for ingestion adapters) → `HttpPort`
- Clock → `ClockPort` (for determinism in tests)

Concrete adapters (`AnthropicLLMAdapter`, `OpenAICompatibleLLMAdapter`,
`PGVectorEmbeddingStore`, etc.) live at the edges and are wired in by
a composition root at startup. This is what makes plugins, testing,
and provider-swapping tractable.

### 1.3 Async-first, with sync escape hatches

The engine is async end-to-end. Ingestion, extraction, search — all
async. Concurrency control lives in the services, not in the tools.

However, plugin authors get both: async APIs by default, with sync
wrappers available for authors uncomfortable with async Python. The
runtime handles thread-pool execution of sync plugin entry points.

### 1.4 "Boring technology" everywhere feasible

- Python 3.11+ (native exception groups, better asyncio, good LLM SDKs).
- Postgres 15+ with pgvector and pg_trgm.
- SQLAlchemy 2.x Core (not ORM) + asyncpg.
- Pydantic v2 for all data validation.
- Alembic for migrations.
- Typer for CLI.
- pytest + pytest-asyncio + testcontainers for testing.
- Ruff for linting.
- The official MCP Python SDK.

One unusual choice: SQLAlchemy Core, not the ORM. Rationale in §7.

### 1.5 Everything is versioned, nothing is destructive

Schemas, extractions, embeddings — all version-tagged. New versions
coexist with old. The user can always diff, audit, or roll back. This
principle shapes a lot of the data model and migration strategy.

---

## 2. Repository and package layout

One monorepo holds the core + official packs. Community packs live in
separate repos.

```
research-engine/                          # top-level repo
├── pyproject.toml                        # workspace root
├── README.md
├── LICENSE                               # Apache 2.0
├── docs/                                 # specs from prior handoff
├── packages/
│   ├── core/                             # the engine
│   │   ├── pyproject.toml
│   │   └── src/research_engine/
│   │       ├── __init__.py
│   │       ├── config/                   # settings, env loading
│   │       ├── domain/                   # domain model (pure, no I/O)
│   │       │   ├── documents.py
│   │       │   ├── passages.py
│   │       │   ├── entities.py
│   │       │   ├── events.py
│   │       │   ├── extractions.py
│   │       │   └── provenance.py
│   │       ├── ports/                    # abstract interfaces
│   │       │   ├── llm.py
│   │       │   ├── embedding.py
│   │       │   ├── reranker.py
│   │       │   ├── http.py
│   │       │   ├── clock.py
│   │       │   └── repositories.py
│   │       ├── adapters/                 # concrete implementations
│   │       │   ├── llm/
│   │       │   │   ├── anthropic.py
│   │       │   │   └── openai_compatible.py
│   │       │   ├── embedding/
│   │       │   │   ├── local_bge.py
│   │       │   │   └── remote_api.py
│   │       │   ├── reranker/
│   │       │   ├── http/
│   │       │   │   └── httpx_adapter.py
│   │       │   └── storage/
│   │       │       ├── postgres/
│   │       │       │   ├── engine.py     # SQLAlchemy engine setup
│   │       │       │   ├── schema.py     # table definitions
│   │       │       │   └── repositories/
│   │       │       │       ├── documents.py
│   │       │       │       ├── passages.py
│   │       │       │       └── ...
│   │       │       └── migrations/       # Alembic
│   │       ├── services/                 # application services
│   │       │   ├── ingestion/
│   │       │   │   ├── orchestrator.py
│   │       │   │   ├── dispatch.py
│   │       │   │   ├── pipeline.py
│   │       │   │   └── chunking/
│   │       │   ├── search/
│   │       │   │   ├── hybrid.py
│   │       │   │   ├── fusion.py
│   │       │   │   └── rerank.py
│   │       │   ├── extraction/
│   │       │   │   ├── executor.py
│   │       │   │   ├── schemas.py
│   │       │   │   ├── validation.py
│   │       │   │   └── caching.py
│   │       │   ├── entities/
│   │       │   ├── events/
│   │       │   ├── provenance/
│   │       │   └── llm_calls/            # LLM logging and accounting
│   │       ├── plugins/                  # the plugin system
│   │       │   ├── manifest.py           # manifest parsing/validation
│   │       │   ├── loader.py             # discovery and loading
│   │       │   ├── registry.py           # type registry (all contributed types)
│   │       │   ├── permissions.py        # permission model
│   │       │   ├── installer.py          # install/uninstall from git URLs
│   │       │   └── sdk/                  # the Plugin SDK (public surface)
│   │       │       ├── __init__.py
│   │       │       ├── types.py
│   │       │       ├── interfaces.py
│   │       │       ├── clients.py        # scoped service clients
│   │       │       ├── decorators.py
│   │       │       └── testing.py
│   │       ├── modules/                  # built-in ingestion modules
│   │       │   ├── pdf_text.py
│   │       │   ├── plain_text.py
│   │       │   ├── markdown.py
│   │       │   ├── epub.py
│   │       │   ├── html.py
│   │       │   └── tei_xml.py
│   │       ├── mcp/                      # MCP server & core tools
│   │       │   ├── server.py
│   │       │   ├── tools/
│   │       │   │   ├── find_passages.py
│   │       │   │   ├── extract.py
│   │       │   │   ├── events.py
│   │       │   │   └── ...
│   │       │   └── dispatch.py           # tool registration for core + plugins
│   │       ├── cli/                      # Typer CLI
│   │       │   ├── main.py
│   │       │   ├── ingest.py
│   │       │   ├── search.py
│   │       │   ├── plugin.py
│   │       │   ├── serve.py
│   │       │   └── backup.py
│   │       ├── composition.py            # composition root (DI wiring)
│   │       └── runtime.py                # runtime entrypoint
│   ├── sdk/                              # published Plugin SDK
│   │   ├── pyproject.toml
│   │   └── src/research_engine_sdk/      # thin re-export of core.plugins.sdk
│   └── plugins/                          # official plugins (separate repos post-Phase 2)
│       ├── history/
│       ├── biblical/
│       └── academic/
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── contract/                         # plugin SDK contract tests
│   └── fixtures/
│       ├── corpora/                      # Founders Online sample
│       └── plugins/                      # reference plugins for testing
└── tools/
    ├── dev-postgres/                     # docker-compose for dev DB
    └── eval/                             # search/extraction eval harness
```

### 2.1 Import rules (enforced in CI)

Dependencies flow inward only:

```
cli, mcp ──┐
            ├──▶ services ──▶ domain
adapters ──┤                    ▲
            └──▶ ports ─────────┘
plugins ────▶ plugins.sdk ──▶ ports + domain (read-only types)
```

- `domain/` imports nothing except stdlib.
- `ports/` imports only `domain/`.
- `services/` imports `ports/` and `domain/`, never `adapters/`.
- `adapters/` implements `ports/`; may import `domain/`.
- `plugins/sdk/` re-exports only `ports/` interfaces and `domain/`
  types. Plugin code cannot reach past the SDK.
- `cli/` and `mcp/` depend on `services/` via the composition root;
  they never create adapters directly.

This is enforced with an import linter rule (`import-linter` / Tach).
Violations fail CI.

### 2.2 Why SDK is a separate package

`packages/sdk` is published to PyPI as `research-engine-sdk`. Plugin
authors depend only on the SDK, not on `research-engine` core. This
ensures:

- Plugin authors see a minimal, stable surface.
- Core can change internally without breaking plugins.
- Plugin CI runs against just the SDK — lightweight.

---

## 3. Composition and runtime

### 3.1 The composition root

One file (`composition.py`) wires the application together. It's the
only place concrete adapters are instantiated. Everything else
receives its dependencies injected.

```python
# packages/core/src/research_engine/composition.py

from dataclasses import dataclass
from research_engine.config import Settings
from research_engine.ports import (
    LLMPort, EmbeddingPort, RerankerPort, HttpPort, ClockPort,
    DocumentRepo, PassageRepo, EntityRepo, EventRepo, ExtractionRepo,
    LLMCallLogRepo, InstalledPluginRepo,
)
from research_engine.adapters.llm.anthropic import AnthropicLLMAdapter
from research_engine.adapters.embedding.local_bge import LocalBGEEmbedding
from research_engine.adapters.reranker.local_bge import LocalBGEReranker
from research_engine.adapters.http.httpx_adapter import HttpxAdapter
from research_engine.adapters.storage.postgres import (
    build_engine, PGDocumentRepo, PGPassageRepo, …
)
from research_engine.services.ingestion import IngestionOrchestrator
from research_engine.services.search import HybridSearchService
from research_engine.services.extraction import ExtractionExecutor
from research_engine.services.entities import EntityService
from research_engine.services.events import EventService
from research_engine.plugins import PluginLoader, PluginRegistry

@dataclass(frozen=True)
class Container:
    settings: Settings
    llm: LLMPort
    embedding: EmbeddingPort
    reranker: RerankerPort
    http: HttpPort
    clock: ClockPort
    docs: DocumentRepo
    passages: PassageRepo
    entities: EntityRepo
    events: EventRepo
    extractions: ExtractionRepo
    llm_calls: LLMCallLogRepo
    installed_plugins: InstalledPluginRepo
    ingestion: IngestionOrchestrator
    search: HybridSearchService
    extraction: ExtractionExecutor
    entity_service: EntityService
    event_service: EventService
    plugin_loader: PluginLoader
    plugin_registry: PluginRegistry

async def build_container(settings: Settings) -> Container:
    sql_engine = await build_engine(settings.db_url)

    # Repositories
    docs = PGDocumentRepo(sql_engine)
    passages = PGPassageRepo(sql_engine)
    entities = PGEntityRepo(sql_engine)
    events = PGEventRepo(sql_engine)
    extractions = PGExtractionRepo(sql_engine)
    llm_calls = PGLLMCallLogRepo(sql_engine)
    installed_plugins = PGInstalledPluginRepo(sql_engine)

    # External ports
    llm = AnthropicLLMAdapter(settings.anthropic_api_key, llm_calls)
    embedding = _build_embedding(settings)
    reranker = _build_reranker(settings)
    http = HttpxAdapter()
    clock = SystemClock()

    # Plugin registry — populated after loader runs
    registry = PluginRegistry()

    # Services
    entity_service = EntityService(entities, registry)
    event_service = EventService(events, registry)
    extraction_service = ExtractionExecutor(
        llm=llm, passages=passages, extractions=extractions,
        registry=registry, entity_service=entity_service,
    )
    search_service = HybridSearchService(
        passages=passages, embedding=embedding, reranker=reranker,
    )
    ingestion_service = IngestionOrchestrator(
        docs=docs, passages=passages, embedding=embedding,
        registry=registry, http=http,
    )

    # Plugin loader wires plugin contributions into registry + services
    plugin_loader = PluginLoader(
        installed_plugins=installed_plugins,
        registry=registry,
        ingestion=ingestion_service,
        extraction=extraction_service,
        search=search_service,
        entity_service=entity_service,
        event_service=event_service,
        llm=llm, http=http,
        settings=settings,
    )
    await plugin_loader.load_enabled()

    return Container(
        settings=settings, llm=llm, embedding=embedding,
        reranker=reranker, http=http, clock=clock,
        docs=docs, passages=passages, entities=entities,
        events=events, extractions=extractions, llm_calls=llm_calls,
        installed_plugins=installed_plugins,
        ingestion=ingestion_service, search=search_service,
        extraction=extraction_service,
        entity_service=entity_service, event_service=event_service,
        plugin_loader=plugin_loader, plugin_registry=registry,
    )
```

This pattern:

- Makes dependencies explicit and testable.
- Has exactly one place to change when swapping an adapter.
- Ensures plugins get scoped clients (next section).

### 3.2 CLI and MCP entrypoints

Both CLI and MCP construct the Container at startup and then act as
thin translation layers.

```python
# cli/main.py (abbreviated)
@app.command()
def ingest(sources: list[Path], plugin: str | None = None):
    asyncio.run(_run_ingest(sources, plugin))

async def _run_ingest(sources, plugin):
    container = await build_container(load_settings())
    try:
        await container.ingestion.ingest_paths(sources, plugin_hint=plugin)
    finally:
        await container.close()
```

```python
# mcp/server.py (abbreviated)
async def main():
    container = await build_container(load_settings())
    server = build_mcp_server(container)
    await server.serve_stdio()
```

---

## 4. The plugin system — concrete architecture

The plugin system is the part of the architecture that most shapes
long-term extensibility. This section goes deep.

### 4.1 What a plugin is, physically

A plugin is a Python distribution (installable with pip) with:

1. A `pack.yaml` manifest at the repo root.
2. Optionally, a Python module with registered contributions.
3. Optionally, non-code assets (schemas, vocabularies, fixtures).

Installing a plugin *from a git URL* means:

1. `git clone` into `$DATA_DIR/plugins/<name>@<version>/`.
2. Read the manifest.
3. Validate.
4. Install into an **isolated Python environment** (see §4.5 below).
5. Register in `installed_plugins` table.
6. Load on next engine start (or live, if hot-reload is supported for
   this plugin type).

### 4.2 The manifest, as parsed

The `pack.yaml` from docs/07 is parsed into a Pydantic model:

```python
# plugins/manifest.py

class PluginCompatibility(BaseModel):
    core_api: str                          # e.g. ">=1.0.0,<2.0.0"
    python: str = ">=3.11"
    plugins: list[PluginDep] = []

class PluginPermissions(BaseModel):
    network: NetworkPerm = NetworkPerm.none    # none | egress | full
    network_allowlist: list[str] = []          # domain allowlist if egress
    llm: bool = False
    filesystem: FilesystemPerm = FilesystemPerm.default
    subprocess: bool = False

class DocumentTypeContribution(BaseModel):
    id: str
    schema: Path                            # relative to plugin dir
    default_chunker: str
    default_ingestion_module: str | None = None
    post_hooks: list[str] = []              # "module.path:func"

class EntityTypeContribution(BaseModel):
    id: str
    schema: Path

class IngestionModuleContribution(BaseModel):
    id: str
    entry: str                              # "module.path:ClassName"

class ExtractionSchemaContribution(BaseModel):
    id: str
    version: int
    file: Path

class MCPToolContribution(BaseModel):
    id: str
    entry: str
    description: str

class PluginContributions(BaseModel):
    document_types: list[DocumentTypeContribution] = []
    entity_types: list[EntityTypeContribution] = []
    event_types: list[EventTypeContribution] = []
    relation_types: list[RelationTypeContribution] = []
    ingestion_modules: list[IngestionModuleContribution] = []
    chunkers: list[ChunkerContribution] = []
    extraction_schemas: list[ExtractionSchemaContribution] = []
    mcp_tools: list[MCPToolContribution] = []
    post_ingestion_hooks: list[HookContribution] = []
    vocabularies: list[VocabularyContribution] = []

class PluginManifest(BaseModel):
    name: str
    version: str
    author: str
    description: str
    license: str
    homepage: str | None = None
    requires: PluginCompatibility
    permissions: PluginPermissions
    provides: PluginContributions
```

### 4.3 The central registry

A single in-memory `PluginRegistry` holds all registered contributions.
It's the runtime catalog.

```python
class PluginRegistry:
    def __init__(self):
        self._document_types: dict[str, DocumentTypeSpec] = {}
        self._entity_types: dict[str, EntityTypeSpec] = {}
        self._event_types: dict[str, EventTypeSpec] = {}
        self._relation_types: dict[str, RelationTypeSpec] = {}
        self._ingestion_modules: dict[str, IngestionModuleFactory] = {}
        self._chunkers: dict[str, ChunkerFactory] = {}
        self._extraction_schemas: dict[tuple[str,int], ExtractionSchema] = {}
        self._mcp_tools: dict[str, MCPToolHandler] = {}
        self._post_ingestion_hooks: dict[str, list[HookFn]] = {}
        self._owners: dict[tuple[str,str], str] = {}  # (kind, id) → plugin

    def register_ingestion_module(self, spec, plugin_name):
        key = ("ingestion_module", spec.id)
        if key in self._owners and self._owners[key] != plugin_name:
            raise PluginConflict(spec.id, self._owners[key], plugin_name)
        self._ingestion_modules[spec.id] = spec
        self._owners[key] = plugin_name

    def resolve_ingestion_module(self, id: str) -> IngestionModuleFactory:
        try:
            return self._ingestion_modules[id]
        except KeyError:
            raise UnknownType(
                "ingestion_module", id,
                hint="Is the providing plugin enabled?",
            )
    # ... similar for each contribution kind
```

Write paths (ingest, extract, etc.) validate types against the
registry before touching the database. An unknown type produces a
clear, actionable error.

### 4.4 The plugin loader — phases

Loading a plugin is a pipeline:

```
┌────────────────────┐
│ 1. Discover        │  Read $DATA_DIR/plugins/; match installed_plugins.
└─────────┬──────────┘
          ▼
┌────────────────────┐
│ 2. Validate        │  Parse manifest; check core_api; resolve files.
│    manifest        │
└─────────┬──────────┘
          ▼
┌────────────────────┐
│ 3. Validate        │  Ensure dependencies (other plugins) loaded first.
│    dependency DAG  │  Topological sort.
└─────────┬──────────┘
          ▼
┌────────────────────┐
│ 4. Register types  │  Schemas, entity/event/relation types. No code yet.
└─────────┬──────────┘
          ▼
┌────────────────────┐
│ 5. Load code       │  Import plugin's Python entry points.
└─────────┬──────────┘
          ▼
┌────────────────────┐
│ 6. Register        │  Ingestion modules, chunkers, tools, hooks.
│    contributions   │
└─────────┬──────────┘
          ▼
┌────────────────────┐
│ 7. Run on_install  │  If first load after install: vocabularies, etc.
│    hooks           │
└─────────┬──────────┘
          ▼
┌────────────────────┐
│ 8. Ready           │  Plugin is active in registry.
└────────────────────┘
```

Each plugin's stages 4–7 run inside a plugin-scoped context so that a
failure in one plugin doesn't corrupt the registry state for others.
Partial loads are rolled back: if stage 5 fails, stage 4 is undone.

### 4.5 Python environment isolation

**This is the most important implementation detail for the plugin
ecosystem.** Getting it right protects users from dependency hell.
Getting it wrong means two plugins that both need different versions
of `pandas` can't coexist.

**Approach:** each plugin installed into its own virtual environment
beneath `$DATA_DIR/plugins/<name>@<version>/.venv/`.

**At load time:** the core engine imports plugin code via a sys.path
manipulation scoped per plugin. Concretely:

```python
import importlib.util, sys
from pathlib import Path

def load_plugin_module(plugin_dir: Path, entry: str):
    # entry is "module.path:ClassName" or "module.path:function"
    module_path, attr = entry.split(":")
    venv_site_packages = plugin_dir / ".venv" / "lib" / f"python3.11" / "site-packages"

    # Prepend plugin's site-packages to sys.path for this import
    old_path = sys.path[:]
    sys.path.insert(0, str(venv_site_packages))
    sys.path.insert(0, str(plugin_dir))
    try:
        # Clear module cache for this namespace, then import
        for cached in list(sys.modules):
            if cached.startswith(f"_plugin_{plugin_name}_"):
                del sys.modules[cached]
        spec = importlib.util.find_spec(module_path)
        if spec is None:
            raise PluginLoadError(f"Cannot find {module_path}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return getattr(mod, attr)
    finally:
        sys.path[:] = old_path
```

**Limitation:** this is *not* true isolation. A plugin can still
`import` stdlib modules and reach global state. But it prevents the
most common problem — conflicting third-party dependencies — without
incurring IPC cost. This is the pragmatic v1 tradeoff.

**v2 path:** move to subprocess-per-plugin with IPC. The plugin SDK
surface is already designed to support this transition (see §4.7).

### 4.6 The plugin SDK surface

The SDK (`research_engine_sdk`) is what plugin authors import. It's a
thin, stable re-export layer:

```python
# research_engine_sdk/__init__.py

from research_engine_sdk.types import (
    # Domain types (read-only data classes)
    Document, Passage, Entity, Event, Edge, Mention,
    ExtractionRecord, SourceRef, ParsedDocument,
    DetectionResult, FuzzyDate,
)

from research_engine_sdk.interfaces import (
    # Base classes plugins subclass
    IngestionModule, Chunker, PostIngestionHook,
)

from research_engine_sdk.clients import (
    # Scoped service clients injected at load time
    CorpusClient, ExtractionClient, EntityClient,
    EventClient, LLMClient, HttpClient, Logger,
)

from research_engine_sdk.decorators import tool, hook

from research_engine_sdk.errors import (
    PermissionDenied, UnknownType, ValidationError,
)

__all__ = [...]
```

Plugin authors write:

```python
# in a plugin
from research_engine_sdk import IngestionModule, ParsedDocument, tool, CorpusClient

class MyModule(IngestionModule):
    id = "my_source"
    version = "0.1.0"
    supported_extensions = [".mysrc"]

    async def parse(self, source, config) -> ParsedDocument:
        ...

@tool(id="my_analysis", description="...", input_schema={...})
async def my_tool(corpus: CorpusClient, **args):
    hits = await corpus.find_passages(query=args["query"], k=20)
    ...
    return {"results": ...}
```

### 4.7 Scoped service clients

Plugin tools and hooks receive clients as injected parameters. Each
client is **scoped to the plugin's declared permissions**. This is the
runtime permission enforcement point.

```python
# research_engine_sdk/clients.py (sketch)

class CorpusClient:
    """Read-only access to corpus. Always granted."""
    async def find_passages(self, query, filters=None, k=20, **hybrid): ...
    async def get_document(self, document_id): ...
    async def get_passage_context(self, passage_id, before=0, after=0): ...

class ExtractionClient:
    """Run extractions or query cached extraction records."""
    async def extract(self, passage_ids, schema, options=None): ...
    async def query_records(self, record_type, filters=None, k=100): ...

class LLMClient:
    """Direct LLM invocation — requires permissions.llm = True."""
    async def complete(self, messages, model=None, **opts): ...
    async def structured(self, messages, schema, model=None): ...

class HttpClient:
    """HTTP fetches — requires permissions.network != none.
       Requests are auto-filtered against permissions.network_allowlist."""
    async def get(self, url): ...
    async def post(self, url, json=None): ...

class EntityClient:
    """Create/update entities."""
    async def upsert(self, entity): ...
    async def resolve(self, name, entity_type=None): ...

# ... etc
```

Under the hood, each client is constructed per-plugin with permission
gates wrapping the underlying services:

```python
# plugins/loader.py (sketch)

def build_plugin_clients(plugin: LoadedPlugin, services: CoreServices) -> PluginClients:
    return PluginClients(
        corpus=CorpusClient(services.search, services.docs),  # always
        extraction=ExtractionClient(services.extraction) if plugin.manifest.permissions.llm else None,
        llm=LLMClient(services.llm, caller=plugin.name) if plugin.manifest.permissions.llm else DeniedLLMClient(),
        http=GatedHttpClient(services.http, allowlist=plugin.manifest.permissions.network_allowlist)
             if plugin.manifest.permissions.network != "none" else DeniedHttpClient(),
        entity=EntityClient(services.entity_service),
        event=EventClient(services.event_service),
        logger=Logger(f"plugin:{plugin.name}"),
    )
```

A plugin that tries to use a client it wasn't granted gets a
`PermissionDenied` exception with a clear explanation of which
permission is missing and how to declare it.

This is also the seam that makes the v1→v2 transition possible: replace
`CorpusClient` with a proxy that forwards calls over IPC to the core
process, and plugin code is unaffected.

---

## 5. Ingestion — concrete implementation

The ingestion pipeline is where third-party integrations like
`logos.com`-style research platforms plug in. This is worth going
deep on because it's the first thing a specialty-domain pack author
typically writes.

### 5.1 The ingestion module interface

```python
# research_engine_sdk/interfaces.py

class IngestionModule(ABC):
    """Parses a source into a ParsedDocument. Does not chunk or embed."""

    id: ClassVar[str]                           # e.g. "pdf_text"
    version: ClassVar[str]
    supported_extensions: ClassVar[list[str]] = []
    supported_mime_types: ClassVar[list[str]] = []

    @abstractmethod
    async def detect(self, source: SourceRef) -> DetectionResult:
        """Returns (confidence, reason) for whether this module handles source."""

    @abstractmethod
    async def parse(
        self,
        source: SourceRef,
        config: dict,
        ctx: IngestionContext,
    ) -> ParsedDocument:
        """Parses the source into a ParsedDocument."""

    def default_chunker(self) -> str:
        return "prose_window"

    def default_document_type(self) -> str:
        return "generic"

    def metadata_schema(self) -> dict:
        """JSON Schema for extended metadata this module produces."""
        return {}
```

`IngestionContext` gives the module access to the HTTP client (if
permitted), logger, and any pack-declared state.

### 5.2 The orchestration pipeline

```python
class IngestionOrchestrator:
    async def ingest_paths(self, paths, plugin_hint=None):
        run = await self._start_run(paths, plugin_hint)
        async with self._item_pool(concurrency=self.settings.ingest_concurrency) as pool:
            async for source in self._discover(paths):
                pool.submit(self._ingest_one(run, source, plugin_hint))
        await self._finalize_run(run)

    async def _ingest_one(self, run, source, hint):
        item = await self._record_item(run, source)
        try:
            module = await self._dispatch(source, hint)
            parsed = await module.parse(source, self._config(module), ctx=IngestionContext(...))
            doc_meta = await self._merge_metadata(source, parsed)
            self._registry.validate_document_type(parsed.document_type, doc_meta)
            passages = await self._chunk(parsed, module)

            async with self._tx() as tx:
                doc = await self._docs.insert(tx, parsed, doc_meta, hash=source.content_hash)
                await self._passages.insert_many(tx, doc.id, passages)
                await self._fts.index_many(tx, passages)
                await self._embeddings.embed_and_store(tx, passages)
                await self._record_provenance(tx, doc, source, module)
            await self._run_post_hooks(doc, parsed)
            await self._record_item_ok(item, doc.id)
        except IngestionError as e:
            await self._record_item_failed(item, e)
            self._logger.warning("ingestion_failed", extra={"source": source.ref, "error": str(e)})
```

Critical properties:

- **Per-document transaction.** Each document is all-or-nothing.
- **Batch isolation.** One failure doesn't abort the batch.
- **Resumability.** `ingestion_items` records mean a `--resume` flag
  can skip completed documents.
- **Idempotency.** Content-hash uniqueness constraint in `documents`
  makes re-ingestion a no-op.

### 5.3 The dispatch algorithm

```python
async def _dispatch(self, source, hint):
    if hint:
        module = self._registry.resolve_ingestion_module(hint)
        if not (await module.detect(source)).is_viable:
            raise DispatchError(f"Module {hint} rejected source {source}")
        return module

    # Try modules in priority order: core built-ins first, then plugins.
    candidates = []
    for module_factory in self._registry.iter_ingestion_modules():
        module = module_factory()
        detection = await module.detect(source)
        if detection.confidence > 0:
            candidates.append((detection.confidence, module))
    if not candidates:
        raise DispatchMiss(source)
    candidates.sort(reverse=True)
    return candidates[0][1]
```

### 5.4 Worked example: a research-platform integration

This is the central example for "how would a Logos-style integration
actually be implemented." The same pattern applies to any
authenticated third-party platform — academic databases, paid
archives, proprietary corpora, etc. The example is deliberately
generic; specific platforms are the pack author's choice and
responsibility.

#### 5.4.1 What a research-platform integration needs to do

A research platform (think: any authenticated scholarly service with
an API) is distinctive because:

1. Sources aren't on disk; they're fetched from an authenticated
   remote API.
2. Each resource has **rich internal structure** the integration
   should preserve (hierarchies, canonical citation schemes, cross-
   references).
3. Content usually has **canonical reference formats** (e.g., verse
   references for biblical software, case citations for legal
   platforms, DOI+section for academic platforms).
4. Access is typically user-authenticated — credentials live in the
   user's installation, not in the plugin.

#### 5.4.2 The plugin structure

```
example-platform-plugin/
├── pack.yaml
├── schemas/
│   ├── document_types/
│   │   └── platform_resource.json
│   └── extraction_schemas/
│       └── resource_references.yaml
├── code/
│   ├── ingestion/
│   │   ├── platform_api.py        # API client
│   │   └── platform_module.py     # IngestionModule
│   ├── auth/
│   │   └── credentials.py         # secure credential storage
│   └── tools/
│       └── platform_search.py
└── tests/
```

`pack.yaml`:

```yaml
name: example-platform
version: 0.1.0
author: "..."
description: "Ingest resources from an authenticated research platform."
license: MIT

requires:
  core_api: ">=1.0.0,<2.0.0"

permissions:
  network: egress
  network_allowlist:
    - api.example-platform.com
  llm: false
  filesystem:
    read: [corpus, plugin_data]
    write: [plugin_data]

provides:
  document_types:
    - id: platform_resource
      schema: schemas/document_types/platform_resource.json
      default_chunker: structural

  ingestion_modules:
    - id: platform_api
      entry: code.ingestion.platform_module:PlatformModule

  mcp_tools:
    - id: platform_search
      entry: code.tools.platform_search:tool
      description: "Search user's platform library directly."
```

#### 5.4.3 Credentials

Plugins do not handle credential storage themselves. The core SDK
provides a secret-storage facility backed by the OS keyring (via the
`keyring` library on macOS/Windows, libsecret on Linux):

```python
# in plugin code
from research_engine_sdk import CredentialsClient

class PlatformModule(IngestionModule):
    id = "platform_api"
    async def parse(self, source, config, ctx):
        creds = await ctx.credentials.get("example-platform")
        if not creds:
            raise PluginConfigError(
                "No credentials configured. Run: "
                "research-engine plugin config example-platform --set api-key=..."
            )
        client = PlatformAPIClient(creds, http=ctx.http)
        raw = await client.fetch_resource(source.platform_id)
        return self._to_parsed_document(raw)
```

Users configure credentials via CLI:

```bash
research-engine plugin config example-platform --set api-key=...
```

The CLI writes through to the OS keyring. Plugin code can only read
credentials *for its own namespace*; it cannot read other plugins'
secrets. This is enforced by the SDK — `ctx.credentials` is pre-scoped
to the calling plugin's name.

#### 5.4.4 Ingestion semantics

For a research platform, `source.ref` is typically a platform-specific
URI like `platform://book/abc123`. The plugin registers a URI scheme
so `research-engine ingest platform://book/abc123` dispatches to the
plugin. The ingestion module then:

1. Fetches the resource via its authenticated client.
2. Walks the platform's structural model (chapters, sections, etc.)
   and builds a `ParsedDocument` preserving locators.
3. Maps platform-native identifiers (e.g., canonical reference IDs)
   into `ParsedDocument.structural_locators` so downstream chunking
   and tools can use them.
4. Attaches platform metadata (edition, publisher, license) to the
   document's metadata.

Structural locators are the key. A passage ingested from a research
platform might have:

```json
{
  "platform_id": "abc123",
  "canonical_ref": "<platform-specific citation>",
  "position_in_resource": 42,
  "section_heading": "..."
}
```

These are what enable domain-specific tools (e.g., "find all
cross-references to this passage") to traverse the corpus later.

#### 5.4.5 Sensitive-source policy reminder

Per `06-ingestion-modules.md`, this kind of plugin is a **Category C**
concern if and only if the platform's Terms of Service prohibit API
access of this kind, or if circumventing protection measures is
required. The main engine repository does not ship such a plugin,
does not link to one, and the maintainers do not advertise one. Users
who have legitimate API access to a platform can write or install
such a plugin at their own responsibility. The architecture supports
them; the project does not promote them.

The example structure above is deliberately generic and illustrates
how *any* authenticated remote source is handled. It is not a
blueprint for any specific service.

### 5.5 Chunking strategies

Chunkers are separate from ingestion modules so they can be reused.
Default chunkers shipped in core:

- `prose_window` — sentence-boundary-aware sliding window, default
  500 tokens with 50-token overlap.
- `whole_or_paragraph` — whole document if ≤ threshold, else by
  paragraph with metadata inherited.
- `structural` — respects document structural decomposition from the
  parser (sections, headings, pericopes, verses — whatever the parser
  emits).
- `fixed_window` — simple character/token windows; fallback for
  edge cases.

Plugins register additional chunkers. Each chunker implements:

```python
class Chunker(ABC):
    id: ClassVar[str]
    version: ClassVar[str]

    @abstractmethod
    async def chunk(self, parsed: ParsedDocument, config: dict) -> list[PassageDraft]:
        ...
```

`PassageDraft` contains text, position, locator, and metadata. The
orchestrator persists it as a `Passage` row.

---

## 6. Search — concrete implementation

### 6.1 Hybrid search pipeline in code

```python
class HybridSearchService:
    async def find_passages(self, query: SearchQuery) -> SearchResult:
        filter_sql = self._build_filter_sql(query.filters)

        async with self._db.connect() as conn:
            candidate_ids = await self._filter_candidates(conn, filter_sql)

        query_vec, query_tsq = await asyncio.gather(
            self._embedding.embed(query.text),
            self._tokenize(query.text),
        )

        vec_task = asyncio.create_task(
            self._vector_search(candidate_ids, query_vec, k=query.k_vec)
        )
        kw_task = asyncio.create_task(
            self._keyword_search(candidate_ids, query_tsq, k=query.k_kw)
        )
        vec_hits, kw_hits = await asyncio.gather(vec_task, kw_task)

        fused = self._fuse(vec_hits, kw_hits, mode=query.fusion_mode, alpha=query.alpha)
        top_n = fused[:query.rerank_n] if query.rerank else fused[:query.k]

        if query.rerank:
            reranked = await self._reranker.rerank(query.text, top_n)
            final = reranked[:query.k]
        else:
            final = top_n[:query.k]

        hydrated = await self._hydrate(final)
        return SearchResult(hits=hydrated, total_candidates=len(candidate_ids))
```

### 6.2 Vector and keyword query specifics

```python
# Vector search (pgvector HNSW)
VECTOR_SQL = """
SELECT p.id AS passage_id,
       1 - (pe.embedding <=> :qv) AS vec_score
FROM core.passage_embeddings pe
JOIN core.passages p ON p.id = pe.passage_id
WHERE pe.model = :model AND pe.model_version = :mv
  AND p.id = ANY(:candidate_ids)
ORDER BY pe.embedding <=> :qv
LIMIT :k;
"""

# Keyword search (tsvector + ts_rank_cd)
KEYWORD_SQL = """
WITH q AS (SELECT plainto_tsquery(:lang, :query) AS tsq)
SELECT pf.passage_id,
       ts_rank_cd(pf.ts, q.tsq) AS kw_score
FROM core.passage_fts pf, q
WHERE pf.passage_id = ANY(:candidate_ids)
  AND pf.ts @@ q.tsq
ORDER BY kw_score DESC
LIMIT :k;
"""
```

`plainto_tsquery` handles messy user input; switch to `websearch_to_tsquery`
for query syntax with OR/quotes as a future enhancement.

### 6.3 RRF fusion

```python
RRF_K = 60

def rrf_fuse(*ranked_lists: list[Hit]) -> list[ScoredHit]:
    scores: dict[UUID, float] = defaultdict(float)
    breakdowns: dict[UUID, dict] = defaultdict(dict)
    for list_idx, hits in enumerate(ranked_lists):
        for rank, hit in enumerate(hits):
            scores[hit.id] += 1.0 / (RRF_K + rank + 1)
            breakdowns[hit.id][f"list_{list_idx}"] = {
                "rank": rank + 1, "score": hit.score,
            }
    return [
        ScoredHit(id=pid, score=score, breakdown=breakdowns[pid])
        for pid, score in sorted(scores.items(), key=lambda x: -x[1])
    ]
```

### 6.4 Query understanding (optional)

Query understanding is an optional LLM-assisted step that parses
natural-language queries into structured filters. It's off by default
in v1. When enabled, it sits between the MCP tool call and the search
service.

---

## 7. Storage

### 7.1 Why SQLAlchemy Core (not ORM)

- Queries are the hot path; the ORM's overhead and query-generation
  opacity are a liability.
- pgvector and tsvector integrations are cleaner at the Core level.
- Complex hybrid queries (vector + filters + FTS) are more readable
  as explicit SQL than as ORM chain calls.
- No session/identity-map concerns to reason about in async code.

### 7.2 Repository pattern

All storage access goes through repository interfaces defined in
`ports/repositories.py`:

```python
class DocumentRepo(Protocol):
    async def insert(self, tx: Transaction, doc: DocumentDraft) -> Document: ...
    async def get(self, doc_id: UUID) -> Document | None: ...
    async def find_by_hash(self, content_hash: bytes, source: str) -> Document | None: ...
    async def update_metadata(self, doc_id: UUID, patch: dict) -> Document: ...
    async def iter_by_filter(self, filter: DocumentFilter) -> AsyncIterator[Document]: ...
```

Concrete Postgres implementations live in
`adapters/storage/postgres/repositories/`. Each repo takes a
SQLAlchemy engine at construction and uses either the engine or a
transaction-bound connection passed in by the service.

### 7.3 Transaction management

Services handle transactions:

```python
async def _tx(self) -> AsyncContextManager[Transaction]:
    async with self._engine.begin() as conn:
        yield Transaction(conn)
```

Repositories accept a transaction parameter and bind to its
connection, so a service can do:

```python
async with self._tx() as tx:
    doc = await self._docs.insert(tx, draft)
    await self._passages.insert_many(tx, doc.id, passages)
```

All within one atomic transaction.

### 7.4 Migrations

Alembic. Migration files live in
`adapters/storage/postgres/migrations/`. Migrations run automatically
on engine startup if `settings.auto_migrate = True` (default for
development; explicit opt-in for production).

### 7.5 Embedding storage and re-indexing

The `passage_embeddings` table is keyed on `(passage_id, model,
model_version)`. Multiple embedding generations coexist. When a new
embedding model is introduced:

1. User runs `research-engine reindex --embeddings --model=<new>`.
2. Embeddings for the new model are computed and inserted alongside
   the old.
3. Searches default to the new model once a config flag is flipped.
4. Old embeddings remain for rollback; can be deleted with an
   explicit `prune` command.

---

## 8. Extraction — concrete implementation

### 8.1 The executor

```python
class ExtractionExecutor:
    async def execute(
        self,
        passage_ids: list[UUID],
        schema_ref: str,             # "name:version" or inline schema
        options: ExtractionOptions,
    ) -> ExtractionBatch:
        schema = self._resolve_schema(schema_ref)
        self._validate_schema(schema)

        results: list[ExtractionResult] = []
        async with self._semaphore(options.concurrency):
            async for batch in self._batch(passage_ids, size=options.batch_size):
                batch_results = await asyncio.gather(*[
                    self._extract_one(pid, schema, options)
                    for pid in batch
                ])
                results.extend(batch_results)
        return ExtractionBatch(results=results, schema=schema)

    async def _extract_one(self, pid, schema, options):
        passage = await self._passages.get(pid)
        cache_key = self._cache_key(pid, schema, options)

        if not options.force_refresh:
            cached = await self._extractions.get_by_key(cache_key)
            if cached:
                return ExtractionResult.from_cached(cached)

        prompt = self._render_prompt(schema, passage, options)
        try:
            llm_response, llm_call = await self._llm.structured(
                messages=[{"role": "user", "content": prompt}],
                schema=schema.output_schema,
                model=options.llm_model or self._default_model,
                caller=options.caller,
                purpose="extraction",
            )
        except LLMError as e:
            return await self._record_failure(cache_key, e)

        records = llm_response["records"]
        try:
            validated = self._validate(records, schema, passage)
        except ValidationError as e:
            if not options.retry_on_validation_error:
                return await self._record_failure(cache_key, e)
            # One retry with validation feedback
            prompt_retry = self._render_prompt_with_error(schema, passage, e)
            llm_response, llm_call = await self._llm.structured(...)
            records = llm_response["records"]
            validated = self._validate(records, schema, passage)

        post_processed = await self._post_process(validated, passage, schema)
        return await self._persist(cache_key, passage, schema, post_processed, llm_call)
```

### 8.2 Evidence-span validation

The validation step is what keeps the system honest:

```python
def _validate_evidence_spans(records, passage):
    for record in records:
        for field_name, field_spec in record.schema.fields.items():
            if field_spec.type != "evidence_span":
                continue
            span_text = record.data[field_name]
            if span_text not in passage.text:
                # Try fuzzy substring match with whitespace normalization
                normalized_passage = _normalize_ws(passage.text)
                normalized_span = _normalize_ws(span_text)
                if normalized_span not in normalized_passage:
                    raise EvidenceNotFound(
                        field=field_name,
                        passage_id=passage.id,
                        span_text=span_text[:100],
                    )
```

Failed evidence validation triggers the one retry with a clarifying
prompt. Persistent failure surfaces as a per-passage extraction
failure without aborting the batch.

### 8.3 Post-processing

- Resolve `entity_ref` fields via the entity service.
- Convert `fuzzy_date` strings to `{start, end, precision}`.
- Compute byte offsets from evidence-span text.

Each of these is a small, testable pure function.

### 8.4 Observability

Every `_extract_one` invocation produces a structured log event with:

- Passage ID, schema, version
- Cache hit / miss
- LLM model, input tokens, output tokens, cost estimate
- Validation pass/fail
- Duration
- Retry count

These feed into `research-engine status` and a future dashboard.

---

## 9. Background work and concurrency

### 9.1 When do we need a job queue?

For v1, the answer is *deliberately, almost never*. The goals:

- Ingestion runs in-process with bounded concurrency, reporting to
  CLI interactively.
- Extraction over batches is CLI-invokable and tracked as an
  `extraction_batch` record; the CLI streams progress.
- MCP tool calls are synchronous from the agent's perspective. Long
  tools (e.g., large extraction batches) return a batch handle; the
  agent polls a status tool.

### 9.2 The asyncio.TaskGroup pattern

Python 3.11 TaskGroup gives us structured concurrency:

```python
async def _batch_extract(self, passage_ids, schema, options):
    async with asyncio.TaskGroup() as tg:
        tasks = [
            tg.create_task(self._extract_one_with_sem(pid, schema, options))
            for pid in passage_ids
        ]
    return [t.result() for t in tasks]
```

Semaphore-bounded concurrency prevents overwhelming the LLM API.
Per-provider rate limits are tracked and backoff applied.

### 9.3 When we'd introduce a job queue

If any of the following happen, we introduce a proper job queue
(Redis+RQ or PostgreSQL-based with `pg_queue`):

- Long-running extractions routinely exceed CLI session lifetimes.
- Users want MCP tools to kick off jobs and come back later.
- Multi-user deployment appears.

The escape hatch is deliberate: keep v1 simple; add complexity only
when forced.

---

## 10. Configuration

### 10.1 Settings model

```python
class Settings(BaseSettings):
    # Database
    db_url: str = "postgresql+asyncpg://localhost/research_engine"
    auto_migrate: bool = True

    # LLM
    llm_provider: Literal["anthropic", "openai_compatible"] = "anthropic"
    anthropic_api_key: SecretStr | None = None
    openai_compatible_base_url: str | None = None
    openai_compatible_api_key: SecretStr | None = None
    default_llm_model: str = "claude-opus-4-7"

    # Embedding
    embedding_provider: Literal["local_bge", "remote_api"] = "local_bge"
    embedding_model: str = "bge-m3"
    embedding_dim: int = 1024

    # Reranker
    reranker_provider: Literal["local_bge", "cohere", "none"] = "local_bge"

    # Paths
    data_dir: Path = Path.home() / ".research-engine"
    plugins_dir: Path | None = None   # defaults to data_dir / "plugins"

    # Ingestion
    ingest_concurrency: int = 4
    embedding_batch_size: int = 32

    # Extraction
    extraction_concurrency: int = 8
    extraction_retry_on_validation: bool = True

    # Logging
    log_level: str = "INFO"
    log_format: Literal["pretty", "json"] = "pretty"

    class Config:
        env_prefix = "RE_"
        env_file = ".env"
```

Settings are loaded at startup. The CLI and MCP entrypoints both use
the same `Settings` class. Environment variables override the `.env`
file, which overrides defaults.

### 10.2 Plugin-specific config

Per-plugin config lives under the plugin's namespace:

```yaml
# $DATA_DIR/config.yaml
plugins:
  example-platform:
    api_base_url: https://api.example-platform.com
    max_concurrent_requests: 2
```

Accessed via `ctx.config` in plugin code, pre-scoped to the plugin's
namespace.

---

## 11. Testing strategy

### 11.1 Test layers

- **Unit tests** — pure functions, parsers, fusion algorithms,
  validation logic. Run fast, no I/O.
- **Integration tests** — services with real Postgres via
  testcontainers. Also include real embedding calls against a small
  local model to catch integration bugs.
- **Contract tests** — the plugin SDK surface. Every SDK type and
  interface has an example plugin that exercises it; contract tests
  verify the example plugins load and operate correctly.
- **Evaluation tests** — search quality, extraction quality. Gold
  query/relevance sets; extraction fixtures. Run nightly, not on
  every commit.
- **End-to-end tests** — spin up the full stack, ingest a fixture
  corpus, make MCP calls, verify outputs.

### 11.2 Plugin SDK contract tests

A test harness plugins can use to verify their contract compliance:

```python
# in a plugin's tests
import pytest
from research_engine_sdk.testing import contract_test, make_fixture_engine

@pytest.mark.contract
async def test_ingestion_module_contract():
    engine = await make_fixture_engine()
    engine.install_plugin("./")
    await engine.ingest(["tests/fixtures/sample.mysrc"])
    docs = await engine.list_documents()
    assert len(docs) == 1
    assert docs[0].document_type == "platform_resource"
```

`make_fixture_engine()` spins up an ephemeral in-memory-ish engine
using an SQLite variant or a testcontainers Postgres, installs a
minimal core, and exposes a scripting API. This lets plugin authors
write tests without simulating the whole system.

### 11.3 Evaluation harness

`tools/eval/` contains:

- A fixed set of labeled queries against Founders Online fixtures.
- Recall@k, MRR, nDCG@k computations.
- Extraction-quality fixtures: (passage, expected extraction record)
  pairs.
- A regression gate — PRs that lower metrics below a threshold are
  flagged.

---

## 12. Observability and operations

### 12.1 Structured logging

Every log event is a structured JSON record:

```json
{
  "ts": "2026-04-19T18:00:00Z",
  "level": "info",
  "event": "extraction_complete",
  "passage_id": "...",
  "schema": "epistolary_references:2",
  "cache_hit": false,
  "duration_ms": 842,
  "llm_model": "claude-opus-4-7",
  "input_tokens": 450,
  "output_tokens": 120,
  "cost_estimate": 0.008
}
```

Structured logs feed `research-engine status` and can be piped to any
log aggregator the user runs.

### 12.2 LLM call accounting

Every LLM call produces an `llm_calls` row. `status` includes:

- Total cost over the last N days.
- Per-plugin cost breakdown.
- Per-schema cost breakdown.
- Cache hit rate.

Users can set a daily spending cap; when exceeded, new LLM calls are
refused with a clear error until the user explicitly raises it.

### 12.3 Health checks

`research-engine health` verifies:

- Postgres reachable, migrations current.
- LLM provider reachable.
- Embedding model loadable.
- Reranker reachable.
- All enabled plugins loadable.

Used in CI and by users diagnosing setup issues.

---

## 13. Error handling philosophy

### 13.1 Error hierarchy

```python
class ResearchEngineError(Exception): ...

class ConfigurationError(ResearchEngineError): ...
class IngestionError(ResearchEngineError): ...
class DispatchMiss(IngestionError): ...
class ParseError(IngestionError): ...
class ValidationError(ResearchEngineError): ...
class EvidenceNotFound(ValidationError): ...
class LLMError(ResearchEngineError): ...
class LLMProviderDown(LLMError): ...
class LLMRateLimited(LLMError): ...
class PluginError(ResearchEngineError): ...
class PluginLoadError(PluginError): ...
class PluginConflict(PluginError): ...
class PermissionDenied(PluginError): ...
class UnknownType(ResearchEngineError): ...
```

### 13.2 Error principles

- Every user-visible error names the component, the problem, and one
  actionable next step.
- No silent failures. If we couldn't do something, we say so.
- Errors from plugins are wrapped to identify the originating plugin.
- Crashes inside extraction or LLM calls never corrupt DB state;
  transactions ensure atomicity.

---

## 14. Security posture (v1, single-user)

### 14.1 Trust boundaries

- The user trusts the core maintainers (by installing the engine).
- The user trusts plugins they install (by explicitly installing
  them).
- The user does not trust arbitrary documents in their corpus — ingest
  pipelines assume hostile input and parse defensively.
- The user does not trust remote responses from LLM providers — all
  outputs are validated before storage.

### 14.2 Sensitive data

- LLM API keys and plugin credentials go through the OS keyring.
- Plugin code can only access credentials in its own namespace.
- `research-engine audit-access` reports what data has been sent to
  external APIs, when, and by which component.

### 14.3 What's explicitly deferred

- Sandboxed plugin execution (deferred to v2).
- Multi-user permissioning (deferred; not a v1 use case).
- Signed plugin manifests (deferred; v1 uses commit-SHA pinning).

---

## 15. Performance notes

### 15.1 Known hot paths

- **Hybrid search under filters.** Filter pushdown is essential; don't
  ANN the whole index then filter.
- **Embedding at ingest.** Batch aggressively; a single-item
  embedding call is a waste of HTTP round-trip.
- **Extraction batch scans.** Cache lookups must hit indexes; `sha256`
  keys are `bytea` with `btree` indexes, not `text`.

### 15.2 Benchmarks to maintain

Automated perf tests run weekly against the Founders Online fixture
corpus (well-known size). Regressions of >20% on any benchmark block
release.

### 15.3 Scale ceilings we accept

v1 targets single-researcher corpora. 1M passages is the soft ceiling;
beyond that, we expect users will either partition their library or
wait for v2 scaling work. This is fine — no Phase 1 use case
approaches it.

---

## 16. What this architecture makes easy

- **Adding a new domain specialty.** Write a plugin with document
  types, entity types, and a handful of tools. No core changes.
- **Adding a new ingestion source** (including authenticated research
  platforms). Implement `IngestionModule`, declare in manifest,
  install. No core changes.
- **Swapping LLM provider.** Implement `LLMPort`; wire in
  composition root. No service changes.
- **Swapping embedding model.** Add new rows to `passage_embeddings`
  for the new model; flip a config flag.
- **Swapping reranker.** Implement `RerankerPort`; wire in.
- **Swapping vector store.** Implement the passage-embeddings repo
  against a new backend; rewire. Harder than the others but still
  contained.

## 17. What this architecture explicitly does not try to solve

- **Multi-user collaboration.** Single-user model throughout. Adding
  multi-user is a v2+ project with its own design doc.
- **Distributed scale.** One machine, one Postgres. Horizontal scale
  would require significant rework.
- **Strong plugin sandboxing.** Trust model is permission-declared
  +user-approved in v1 (see OQ-4).
- **Cross-platform GUI.** CLI + MCP only.

---

## 18. First 30 days of implementation

For the engineering team kicking this off, a concrete sequence:

**Week 1:**
- Project skeleton, linting, CI.
- Postgres + pgvector via docker-compose.
- Basic domain types (Pydantic).
- Composition root with stub adapters.
- Smoke MCP server with one dummy tool.

**Week 2:**
- SQLAlchemy schema and Alembic migrations.
- Document and Passage repositories.
- Plain-text and Markdown ingestion modules (simplest).
- `research-engine ingest` CLI for local files.

**Week 3:**
- Embedding adapter (local BGE).
- FTS index.
- `find_passages` MCP tool with hybrid search and RRF.
- First end-to-end: ingest a directory, search from Claude Code.

**Week 4:**
- PDF and EPUB modules.
- Entity and mention tables + basic resolver.
- `resolve_entity`, `get_document` MCP tools.
- First integration tests.

By week 4 the foundation is working end-to-end. Weeks 5–14 expand
along the lines in `09-roadmap.md`. The plugin system itself doesn't
materialize until Phase 2 — until then, "plugins" are directories
under `packages/plugins/` that use the same SDK but are monorepo-
resident. Factoring them out into separate repos is straightforward
once the interface has stabilized.

---

## Appendix A: Key interfaces in one place

For quick reference. Full definitions live in
`research_engine_sdk/interfaces.py`.

```python
class IngestionModule(ABC):
    id: str; version: str
    supported_extensions: list[str]
    supported_mime_types: list[str]
    async def detect(self, source: SourceRef) -> DetectionResult: ...
    async def parse(self, source: SourceRef, config: dict, ctx: IngestionContext) -> ParsedDocument: ...
    def default_chunker(self) -> str: ...
    def default_document_type(self) -> str: ...
    def metadata_schema(self) -> dict: ...

class Chunker(ABC):
    id: str; version: str
    async def chunk(self, parsed: ParsedDocument, config: dict) -> list[PassageDraft]: ...

class PostIngestionHook(ABC):
    async def run(self, doc: Document, parsed: ParsedDocument, ctx: HookContext) -> None: ...

@tool(id=..., description=..., input_schema=...)
async def my_tool(corpus: CorpusClient, extraction: ExtractionClient, **args) -> dict: ...
```

## Appendix B: Checklist for a new plugin

Given to plugin authors. Useful for the team as internal validation
when writing official packs.

1. Define the problem: what new document types / entity types / tools
   does this pack contribute? Write a one-paragraph description for
   the manifest.
2. Create `pack.yaml` with identity, compatibility, permissions, and
   `provides`.
3. For each contributed ingestion module: implement the interface,
   handle `detect()` correctly, produce a well-formed `ParsedDocument`.
4. For each contributed entity/event/relation type: write a JSON
   Schema; validate sample records against it.
5. For each contributed extraction schema: write the YAML with clear
   record types, required evidence fields, and a well-scoped prompt.
6. For each contributed MCP tool: define input schema, implement the
   handler using injected clients only, never reach past the SDK.
7. Tests: at least one contract test per contribution kind. Fixtures
   that exercise real parse/chunk/extract flows.
8. Docs: README with installation, configuration, usage examples,
   license, and (if applicable) notes on permission requirements.
9. Publish: tag a release on GitHub. Users install with
   `research-engine plugin install <repo>@<tag>`.
