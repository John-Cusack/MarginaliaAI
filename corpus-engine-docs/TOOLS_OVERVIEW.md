# Corpus Engine — Project & Tools Overview

## What This System Is

The Corpus Engine (project name: MarginaliaAI / `marginalia-ai`) is a personal library research engine. It ingests documents from multiple sources, chunks and embeds them, and makes them deeply queryable, extractable, and cross-linkable through an MCP tool interface. It is designed for academic and theological research.

The system has three layers:

1. **Core engine** — hybrid search (keyword + vector + reranking), entity resolution, event store, knowledge graph, structured LLM extraction, and a pluggable ingestion pipeline.
2. **Plugin SDK** — plugins publish a schema-v2 `plugin.yaml` manifest through the `research_engine.plugins` entry-point group. The engine validates it without importing plugin code, then injects protocol-based clients (corpus, entity, event, extraction, ingestion, LLM, HTTP) with permission gating after explicit approval.
3. **Plugins** — four public distributions currently target core/SDK `0.6.x`: history, Logos, academic-journal, and YourCloudLibrary. Kindle is not published.

All tools are exposed as MCP tools through a single server (`research-engine serve`).

---

## Core Engine Tools (17 tools)

### Search & Retrieval

| Tool | What it does |
|------|-------------|
| `find_passages` | Hybrid search over all passages. Supports keyword, vector, and fused retrieval with optional cross-encoder reranking. Filters by document_type, author, date_range, mentioned entities, corpus_tags, and document-type-specific metadata. Hybrid modes: `rrf`, `weighted`, `vector_only`, `keyword_only`. |
| `get_document` | Fetch a document by UUID. Returns metadata and its passages. Optionally includes full concatenated text. |
| `get_passage_context` | Expand around a passage — returns N passages before and after. Use this to read surrounding context of a search hit. |
| `similar_to` | "More like this" — finds passages similar to a known passage using vector similarity. |

### Entities

| Tool | What it does |
|------|-------------|
| `resolve_entity` | Look up entities by name, alias, or attributes. Tiered: exact match → alias match → fuzzy. Returns candidates with scores. |
| `get_entity` | Fetch a full entity record by UUID, including all aliases and attributes. |
| `find_mentions` | Find all passages that mention a given entity. Filterable by date range and document type. |
| `upsert_entity` | Create or update an entity. Deduplicates by similar name. Fields: `entity_type`, `canonical_name`, `disambiguator`, `aliases`, `attributes`. |

### Events & Timeline

| Tool | What it does |
|------|-------------|
| `events` | Query the event store. Filter by actor entities, event types, date range, location, and payload fields. Group by month/week/day/year/event_type/actor. Supports aggregation. |
| `timeline_compare` | Overlay multiple event streams on a shared time axis for comparative analysis. Each stream has its own filters; results are bucketed by day/week/month. |
| `upsert_event` | Create an event record. Link to actor entities and source passages. Fields: `event_type`, timestamps, `precision` (day→decade), `location_id`, `payload`, `actors` (with roles). |

### Extraction

| Tool | What it does |
|------|-------------|
| `extract` | Run a registered or ad-hoc extraction schema against passages. Extractions are cached; use `force_refresh` to re-extract. Accepts explicit passage IDs or a passage filter. |
| `list_extraction_schemas` | List all registered extraction schemas (name:version). |
| `query_extractions` | Retrieve previously-extracted records without re-running extraction. Filter by record type, passage filter, and extracted data fields. |

### Knowledge Graph & Provenance

| Tool | What it does |
|------|-------------|
| `upsert_edge` | Create a directed relationship edge between any two nodes (entity, document, passage, or event). Fields: `relation_type`, `source_kind/id`, `target_kind/id`, `attributes`, `confidence`. |
| `provenance_of` | Trace the provenance chain for any derived data item (extraction record, mention, or event) back to its source passage and document. |

### Corpus Metadata

| Tool | What it does |
|------|-------------|
| `corpus_stats` | Corpus shape and coverage statistics. Document counts, passage counts, breakdowns by type and author, date coverage, language distribution. Filterable. |

---

## Published Plugin Tools

Plugin manifests and the live MCP tool catalogue are authoritative. The lists
below identify the main workflows, not a second frozen copy of every input
schema. An agent should read the description and schema supplied by MCP before
calling a tool.

Install and approve plugins using the
[published-plugin guide](../README.md#published-plugins), then restart
`research-engine serve`. A wheel being installed does not make its tools
available until the exact artifact has been audited and enabled.

### History Plugin (`history`, 2 tools)

Adds correspondence analysis plus historical document, entity, event,
relationship, and extraction-schema contributions.

| Tool | What it does |
|------|-------------|
| `history.find_missing_letters` | Detect likely missing letters between two correspondents. |
| `history.correspondence_cadence` | Analyze correspondence density and cadence between two entities. |

### Logos Bible Software Plugin (`logos`, 28 tools)

Provides Logos passage, study, library, lexicon, search, authentication, and
licensed-book ingestion workflows. It requires a licensed Logos account for
protected resources. Browser authentication is explicit through `logos-login`;
state lives under the plugin data directory, not inside site-packages.

| Tool | What it does |
|------|-------------|
| `logos.passage_text` | Fetch Bible passage text in one or more translations. |
| `logos.passage_guide` | Retrieve passage-guide material including commentary and cross-references. |
| `logos.word_study` | Run a Hebrew or Greek word study. |
| `logos.get_entry` | Retrieve the full verbatim text of a lexicon or dictionary entry. |
| `logos.search` | Full-text search across the authenticated Logos library. |
| `logos.library` | Search library metadata and obtain resource IDs. |
| `logos.ingest_book` | Ingest a licensed Logos resource resumably into the corpus. |
| `logos.auth_status` | Report whether the saved Logos session is usable. |

Use the other `logos.*` tools advertised by MCP for reference resolution,
commentary, parallels, Factbook, notes, ingestion status, diagnostics, and
scholar-authority workflows.

### Academic Journal Plugin (`academic-journal`, 9 tools)

Discovers papers through OpenAlex, Semantic Scholar, and Crossref; resolves
legal open-access copies; ingests them; extracts bibliographies; and records
citation edges. Its staged workers and plugin-owned database must be migrated
before use.

| Tool | What it does |
|------|-------------|
| `academic-journal.discover_papers` | Search configured scholarly metadata providers by query. |
| `academic-journal.discover_by_doi` | Resolve one paper by DOI. |
| `academic-journal.discover_by_author` | Discover papers by author name or OpenAlex ID. |
| `academic-journal.search_papers` | Search papers already ingested into the corpus. |
| `academic-journal.import_manual_pdf` | Import a PDF the operator acquired legally. |
| `academic-journal.pipeline_status` | Report paper stages, queue depth, provider health, and workers. |
| `academic-journal.retry_failed` | Requeue failed pipeline work. |
| `academic-journal.start_workers` | Start the in-process acquisition and ingestion workers. |
| `academic-journal.stop_workers` | Stop those workers. |

The plugin also contributes the `acad` live source-search provider,
`academic_paper` filtering, bibliography extraction, and citation hooks.

### YourCloudLibrary Plugin (`yourcloudlibrary`, 10 tools)

Searches a participating library's catalog and ingests books the user is
authorized to borrow. Run `research-engine-ycl-login` and install its matching
Playwright Chromium build before authenticated use.

| Tool | What it does |
|------|-------------|
| `yourcloudlibrary.auth_status` | Report whether the saved library session can search and read. |
| `yourcloudlibrary.search_catalog` | Search the whole catalog with live availability. |
| `yourcloudlibrary.acquire_and_ingest` | Borrow, scrape, ingest, and optionally return a catalog book safely. |
| `yourcloudlibrary.ingest_book` | Ingest a book that is already on loan. |
| `yourcloudlibrary.sync_loans` | Synchronize active loans and their due dates. |
| `yourcloudlibrary.list_books` | List known active or historical loans. |

The remaining `yourcloudlibrary.*` tools advertised by MCP inspect, scrape,
record, or forget individual books. The plugin also contributes a live
source-search provider and the `ycl_book` document type.

Kindle is deliberately absent from this catalogue because it has no public
PyPI release.

---

## Plugin Document Types and Extraction Schemas

These are the contributions relevant when filtering or extracting from
plugin-ingested material:

| Kind | ID | Plugin | Purpose |
|------|----|--------|---------|
| Document type | `letter` | History | Historical correspondence |
| Document type | `logos_book` | Logos | Licensed Logos resources with scripture-aware chunking |
| Document type | `ycl_book` | YourCloudLibrary | Borrowed ebooks chunked as prose |
| Paper filter | `academic_paper` | Academic journal | Filter papers by year, venue, citation count, or open-access state |
| Extraction schema | `epistolary_references:1` | History | Correspondence references |
| Extraction schema | `claims:1` | History | Claims and their evidence |
| Extraction schema | `scripture_cross_refs:1` | Logos | Scripture cross-references |
| Extraction schema | `bibliography_references:1` | Academic journal | Bibliography references |

---

## Key Patterns for Using These Tools

**Finding content**: Start with `find_passages` for corpus search and
`similar_to` for "more like this." Use `logos.search`,
`academic-journal.search_papers`, or `yourcloudlibrary.search_catalog` when the
source-specific service is the authority. Use `get_passage_context` around
corpus hits.

**Studying a Bible passage**: Use `logos.passage_text` for the text,
`logos.exegetical_guide` for original-language analysis,
`logos.commentary` for scholarly commentary, and
`logos.cross_references` or `logos.parallel_passages` for related texts.

**Building knowledge**: Use `upsert_entity` to track
people/places/concepts, `upsert_event` for temporal events, and `upsert_edge`
for relationships. Use `extract` with schemas to pull structured data from
passages at scale.

**Ingesting new content**: Use `logos.library` followed by
`logos.ingest_book`; use `academic-journal.discover_papers`,
`academic-journal.start_workers`, and
`academic-journal.pipeline_status` for papers; or use
`yourcloudlibrary.search_catalog` followed by
`yourcloudlibrary.acquire_and_ingest` for an authorized library loan.

**Correspondence gaps**: Use `history.find_missing_letters` and
`history.correspondence_cadence` after correspondence entities and events have
been extracted.

**Provenance**: Every extraction record, mention, and event can be traced back to its source passage and document via `provenance_of`.
