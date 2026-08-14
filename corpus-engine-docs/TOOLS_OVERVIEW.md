# Corpus Engine — Project & Tools Overview

## What This System Is

The Corpus Engine (project name: MarginaliaAI / `research-engine`) is a personal library research engine. It ingests documents from multiple sources, chunks and embeds them, and makes them deeply queryable, extractable, and cross-linkable through an MCP tool interface. It is designed for academic and theological research.

The system has three layers:

1. **Core engine** — hybrid search (keyword + vector + reranking), entity resolution, event store, knowledge graph, structured LLM extraction, and a pluggable ingestion pipeline.
2. **Plugin SDK** — plugins declare tools, document types, chunkers, and ingestion modules via a `pack.yaml` manifest. The engine injects protocol-based clients (corpus, entity, event, extraction, ingestion, LLM, HTTP) with permission gating.
3. **Plugins** — three installed: Logos Bible Software, Academic Journal, and Kindle.

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

## Logos Bible Software Plugin (25 tools)

Integrates with the Logos web app API (`app.logos.com`). Provides full access to Bible study resources, commentaries, original-language tools, and a two-phase book ingestion pipeline with verse-boundary chunking.

**Auth**: Cookie-based via Playwright login flow, stored at `~/.logos-mcp/cookies.json`.

### Bible Text & Passage Resolution

| Tool | What it does |
|------|-------------|
| `logos.passage_text` | Get Bible passage text. Input: `reference` (Logos format or natural language like "John 3:16"), `versions` (array, default `["LEB"]`). |
| `logos.resolve_passage` | Resolve a natural-language Bible reference to Logos canonical form. Input: `query` (e.g. "John 3:16"). |
| `logos.passage_suggestions` | Typeahead suggestions for partial passage input. Input: `input` (partial text). |

### Study & Exegetical Tools

| Tool | What it does |
|------|-------------|
| `logos.passage_guide` | Comprehensive passage guide — aggregates commentaries, cross-refs, parallels, and study resources for a reference. |
| `logos.exegetical_guide` | Original-language exegetical analysis for a passage. Input: `reference`. |
| `logos.word_study` | Hebrew/Greek word study. Input: `word`. |
| `logos.commentary` | Fetch commentary entries for a passage. Filter by `resource_sets`: BibleCommentaries, StudyBibles, TextualCommentaries, etc. `character_limit` controls response size (default 5000). |
| `logos.cross_references` | Cross-references for a passage. |
| `logos.parallel_passages` | Synoptic and thematic parallels. |
| `logos.relations` | People, places, and things related to a passage. |
| `logos.factbook` | Factbook report for a topic (e.g. "Jesus", "Jerusalem", "Baptism"). |

### Search & Library

| Tool | What it does |
|------|-------------|
| `logos.search` | Full-text search across all Logos books. Inputs: `query`, optional `scope` (resource ID), `limit`. |
| `logos.library` | Search user's Logos library metadata. Filter by `type` (commentary, dictionary, bible). `include_unlicensed` to see unowned resources. |
| `logos.toc` | Get table of contents for a resource. Input: `resource_id`. |

### AI & Notes

| Tool | What it does |
|------|-------------|
| `logos.study_assistant` | Logos AI study assistant (streaming). Input: `message`, optional `conversation_id` for multi-turn. |
| `logos.ai_synopsis` | AI-generated synopsis of search results. Input: `query`. |
| `logos.notes` | Get user notes and highlights for a reference. |

### System

| Tool | What it does |
|------|-------------|
| `logos.credits` | Check feature credit usage (Logos AI credits etc). |
| `logos.auth_status` | Check authentication status. |
| `logos.workflows` | List available workflow templates. |

### Ingestion

| Tool | What it does |
|------|-------------|
| `logos.ingest_book` | Ingest a Logos book into the corpus. Two-phase pipeline: (1) walk the article chain via Logos API, chunk with verse-boundary chunker, checkpoint to plugin DB — resumable from any crash; (2) store to corpus with adaptive batch-halving retry on embedding failures. Inputs: `resource_id`, `max_articles` (0 = all). Passage metadata includes `scripture_refs`, `page_start`, `page_end`, `page_refs`, `volume`, `author`, `heading_path`. |
| `logos.ingest_pdf` | Ingest a local PDF with verse-boundary chunking. Inputs: `path`, `document_type` (default "logos_book"). |

### Scholar Authority Tracking

| Tool | What it does |
|------|-------------|
| `logos.search_scholars` | Search scholar authority records. Filter by `name`, `field` (e.g. "New Testament"), `passage_book` (e.g. "Romans"). |
| `logos.gap_analysis` | Analyze gaps between known scholars and owned resources for a biblical book. Input: `passage_book`. |
| `logos.record_authority` | Record a scholar's authority score for a passage range. Fields: `scholar_name`, `passage_book`, `passage_start/end`, `authority_score` (0-1), `score_reasons`, `work_title`, `series_name`, `series_tier` (1-5). |

---

## Academic Journal Plugin (8 tools)

Discovers, acquires, and ingests academic papers from OpenAlex, Semantic Scholar, and Crossref. Runs a 5-stage background pipeline: discovered → resolved → acquired → ingested → citations_extracted → complete.

**Infrastructure**: Rate-limited HTTP with circuit breakers, PostgreSQL job queue (SELECT FOR UPDATE SKIP LOCKED), acquisition module system with priority-based dispatch.

| Tool | What it does |
|------|-------------|
| `acad.discover_papers` | Search for papers by query. Inputs: `query`, `max_papers` (default 200), `sources` (openalex / semantic_scholar / crossref). |
| `acad.discover_by_doi` | Look up a single paper by DOI. Input: `doi` (e.g. "10.1038/s41586-021-03819-2"). |
| `acad.discover_by_author` | Find papers by author. Inputs: `author_name`, `openalex_author_id`, `max_papers`. |
| `acad.pipeline_status` | Show pipeline status: papers by stage, queue depths, recent errors, API health, worker status. |
| `acad.search_papers` | Search already-ingested papers. Filter by `year_min`, `year_max`, `venue`. |
| `acad.retry_failed` | Re-enqueue failed jobs. Inputs: `stage` (resolved/acquired/ingested/citations_extracted), `error_pattern`. |
| `acad.import_manual_pdf` | Import a manually downloaded PDF for a known paper. Inputs: `file_path`, `paper_id` or `doi`, `title`. |
| `acad.start_workers` | Launch background pipeline workers to process the job queue. |

---

## Kindle Plugin (1 tool)

Scrapes book text from Kindle Cloud Reader via Playwright.

| Tool | What it does |
|------|-------------|
| `kindle.scrape_book` | Scrape full text of a Kindle book by ASIN. Opens a headless browser, authenticates with Amazon, navigates pages with randomized delays, extracts text. Inputs: `book_asin`, `force_reauth`, `page_delay_min/max`, `max_pages`. Output saved to `~/.marginalia/plugins/kindle/extracted/{asin}.txt`. |

---

## Document Types in the Corpus

| Type | Source | Chunker |
|------|--------|---------|
| `logos_book` | Logos plugin | `verse_boundary` — splits on verse reference boundaries, extracts scripture_refs into metadata |
| `kindle_book` | Kindle plugin | `prose_window` — sliding window chunker for prose |
| Academic papers | Academic plugin | Default core chunker |

## Extraction Schemas

| Schema | Plugin | What it extracts |
|--------|--------|-----------------|
| `scripture_cross_refs:1` | Logos | Scripture cross-references from passage text |
| `bibliography_references:1` | Academic | Bibliography references from paper text |

---

## Key Patterns for Using These Tools

**Finding content**: Start with `find_passages` for broad search, `similar_to` for "more like this", or `logos.search` / `acad.search_papers` for source-specific search. Use `get_passage_context` to expand around hits.

**Studying a Bible passage**: Use `logos.passage_text` for the text, `logos.exegetical_guide` for original-language analysis, `logos.commentary` for scholarly commentary, and `logos.cross_references` / `logos.parallel_passages` for related texts.

**Building knowledge**: Use `upsert_entity` to track people/places/concepts, `upsert_event` for temporal events, and `upsert_edge` for relationships. Use `extract` with schemas to pull structured data from passages at scale.

**Ingesting new content**: Use `logos.ingest_book` with a resource ID (find it via `logos.library`), `acad.discover_papers` + `acad.start_workers` for papers, or `kindle.scrape_book` for Kindle books.

**Provenance**: Every extraction record, mention, and event can be traced back to its source passage and document via `provenance_of`.
