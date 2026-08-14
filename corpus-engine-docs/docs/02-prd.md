# 02 — Product Requirements Document

## Scope

This PRD covers Corpus Engine v1, defined as the minimum viable product
that fully supports the McClellan/Barlow research use case end-to-end and
is architected for extension to other domains via packs.

Subsequent versions (v2+) are outlined in the roadmap but not detailed here.

## In scope for v1

- Local installation and operation (macOS and Linux; Windows best-effort).
- Ingestion of PDFs, plain text, Markdown, EPUB, and structured TEI/XML
  out of the box.
- A Postgres + pgvector storage backend.
- Hybrid search (vector + BM25/FTS + metadata filters + optional rerank).
- A generic structured extraction framework powered by LLM calls.
- An event and timeline model with fuzzy date support.
- An entity and mention model with basic alias and disambiguation support.
- A provenance and citation layer that makes every derived fact traceable
  to a source span.
- An MCP server exposing the core toolset to Claude Code and other agents.
- A pack system with manifest-based GitHub installation.
- One reference pack: `history` (driven by the McClellan use case).
- A CLI for corpus administration (ingest, index, pack management, serve).

## Out of scope for v1

- A graphical user interface. All interaction is via CLI + Claude Code
  (or equivalent MCP client).
- Multi-user/team deployment. Corpus Engine v1 is single-user, local.
- Cloud-hosted SaaS offering.
- Mobile clients.
- Built-in LLM hosting. The engine calls an external LLM API (Anthropic
  default, but provider-agnostic).
- A central pack registry with a web UI. Packs are installed by GitHub
  URL only.
- Ingestion of DRM-protected ebook formats in the main repository
  (see §"Sensitive ingestion sources" below).
- Automated OCR of scanned documents in core (can be pre-processed
  externally or handled by a pack).
- Real-time collaborative research features.

## User stories

These are the stories v1 must satisfy. They are written in the voice of
the primary user persona (serious researcher).

### Ingestion

- **U-I-1.** *As a researcher, I can point the engine at a directory of
  PDFs and have them ingested, chunked, embedded, and indexed without
  manual per-file configuration.*
- **U-I-2.** *As a researcher, I can attach a metadata file (YAML/JSON/CSV)
  alongside a batch of documents to pre-populate sender, recipient, date,
  and other fields during ingestion.*
- **U-I-3.** *As a researcher, I can re-ingest a document after correcting
  its metadata without losing any extractions that were performed against
  its passages.*
- **U-I-4.** *As a researcher, I can see ingestion status and errors in the
  CLI, and partially-ingested batches can be resumed.*

### Search

- **U-S-1.** *As a researcher, I can ask Claude Code a natural-language
  question about my corpus and get answers with exact citations.*
- **U-S-2.** *As a researcher, I can apply metadata filters (author,
  recipient, date range, document type) to any search.*
- **U-S-3.** *As a researcher, I can run hybrid search that combines
  semantic similarity with keyword matching, and I can see a score
  breakdown for each hit.*
- **U-S-4.** *As a researcher, I can retrieve the full document context
  around any search hit.*

### Extraction

- **U-E-1.** *As a researcher, I can define a structured schema and have
  the engine extract instances of that schema from passages, with
  provenance spans attached to every extracted record.*
- **U-E-2.** *As a researcher, extractions are cached by passage and
  schema so repeated runs don't re-incur LLM cost.*
- **U-E-3.** *As a researcher, I can re-run an extraction against a newer
  LLM or a refined prompt and see what changed.*

### Events and timelines

- **U-T-1.** *As a researcher, I can query events by type, actor, date
  range, and location, returning structured results.*
- **U-T-2.** *As a researcher, fuzzy dates ("summer 1862", "late July")
  are preserved honestly through query and display.*
- **U-T-3.** *As a researcher, I can overlay multiple event streams
  (letters, battles, claims) on a shared time axis.*

### Gap detection (history pack; proves the extraction framework)

- **U-G-1.** *As a historian, the history pack can extract epistolary
  references from letters and flag referenced-but-absent letters in my
  corpus, with the exact citing span for each candidate.*
- **U-G-2.** *As a historian, I can view correspondence cadence between
  any two parties and see statistically anomalous gaps.*

### Packs

- **U-P-1.** *As a user, I can install a pack from a GitHub URL with a
  single CLI command.*
- **U-P-2.** *As a user, I can see what capabilities a pack adds and what
  permissions it requests before installing.*
- **U-P-3.** *As a pack author, I can scaffold a new pack, develop it
  locally, and install it from my filesystem during development.*
- **U-P-4.** *As a pack author, I can declare entity types, event types,
  extraction schemas, ingestion adapters, and MCP tools in a manifest.*

### Operations

- **U-O-1.** *As a user, I can start an MCP server that Claude Code
  connects to and exposes both core and pack tools.*
- **U-O-2.** *As a user, I can back up my library (data + embeddings +
  extractions) as a portable bundle.*

## Functional requirements

### FR-1: Document ingestion

- Core MUST support ingestion of PDF (text-extractable), plain text,
  Markdown, EPUB, HTML, and TEI/XML source files.
- Core MUST NOT include ingestion for DRM-protected formats, proprietary
  paid-platform formats, or any format whose inclusion would create IP or
  ToS issues. Those live in separate packs (see §"Sensitive ingestion
  sources").
- Ingestion MUST preserve original file alongside extracted text.
- Ingestion MUST record provenance: source path, file hash, ingestion
  timestamp, parser used, parser version.
- Ingestion MUST produce document-level and passage-level records in a
  single transaction per document (either all-or-nothing per document).
- Ingestion errors MUST be logged with enough context to resume or
  debug, and MUST NOT abort batch processing for other documents.

### FR-2: Chunking

- Core MUST provide a default chunking strategy suitable for general prose
  (configurable window size with sentence-boundary snapping).
- Ingestion modules and packs MUST be able to override chunking with
  strategies appropriate to their source type (e.g. whole-letter for
  short letters, pericope for biblical text, section for academic papers).
- Every chunk MUST store: parent document ID, position within document,
  byte offsets or equivalent structural locators, text, and inherited
  metadata.

### FR-3: Metadata

- Every document MUST have a required minimal metadata set: `id`, `title`
  (where available), `source`, `ingested_at`, `content_hash`.
- Every document MAY have extended metadata stored as typed JSONB,
  governed by document_type-specific schemas contributed by packs.
- Metadata MUST be editable post-ingestion without requiring re-ingestion
  of the underlying document.

### FR-4: Entities and mentions

- Core MUST provide a generic entity model supporting type, canonical
  name, aliases, and arbitrary typed attributes.
- Core MUST provide a mention model linking passages to entities with
  confidence scores and optional span locators.
- Entity types MUST be extensible via packs.
- Entity resolution (merging aliases, disambiguating references) MAY be
  assisted by the engine but MUST support manual correction.

### FR-5: Events

- Core MUST provide an event model with event type, fuzzy start timestamp,
  fuzzy end timestamp, precision, actor entity references, optional
  location, and type-specific payload (JSONB).
- Event types MUST be extensible via packs.
- Events MUST be queryable by type, actor, time range, location, and
  payload fields (with indexed fields where useful).

### FR-6: Relationships

- Core MUST provide a generic edge/relationship model connecting
  entity-to-entity, entity-to-document, document-to-document, and
  event-to-event.
- Relation types MUST be extensible via packs.

### FR-7: Hybrid search

- Core MUST provide hybrid search combining vector similarity and
  keyword matching over the passage layer.
- Search MUST support metadata filters applied before or alongside
  similarity ranking.
- Search MUST expose configurable fusion (RRF by default; weighted
  alternatives available).
- Reranking via cross-encoder MUST be available as an optional final
  stage.
- Every hit MUST include score breakdowns (vector, keyword, rerank,
  final) for transparency.

### FR-8: Extraction framework

- Core MUST provide a generic `extract(passage_ids, schema)` primitive
  that invokes an LLM with the schema, validates output, and stores
  typed results with provenance spans.
- Extractions MUST be cached keyed on `(passage_id, schema_hash,
  extractor_version)`.
- Schemas MUST be version-controlled; re-running against a new schema
  version produces a new extraction generation without overwriting the
  old one.
- Every extracted record MUST include the evidence span (byte range or
  text excerpt) from which it was derived.

### FR-9: Provenance

- Core MUST record the chain `source file → parser → document → passage →
  extraction/entity/event` for every derived fact.
- Provenance MUST be queryable: given any derived record, the system can
  return the passage, document, and source span it came from.

### FR-10: MCP server

- Core MUST ship an MCP server exposing the standard tool surface (see
  [05-mcp-spec.md](05-mcp-spec.md)).
- Packs MUST be able to register additional MCP tools that appear in the
  same server.
- The MCP server MUST declare tool schemas in a way Claude Code and
  compatible clients can consume.

### FR-11: Pack system

- Core MUST support installing packs from GitHub URLs, local
  filesystem paths, and pinned commit SHAs.
- Core MUST validate the pack manifest and refuse to load incompatible
  packs (core API version mismatch, unmet dependencies).
- Core MUST enforce declared permissions (network, LLM, filesystem
  scope) against pack code where technically feasible.
- Packs MUST be enable/disable-able at runtime without reinstalling.

### FR-12: CLI

- Core MUST provide a CLI with subcommands for: `init`, `ingest`,
  `reindex`, `search`, `pack install/uninstall/list/enable/disable`,
  `serve`, `backup`, `restore`.
- CLI output MUST be scriptable (JSON output mode on all read commands).

## Non-functional requirements

### NFR-1: Performance

- Hybrid search over a corpus of 100k passages MUST return in <2s P95
  on a modern laptop.
- Ingestion MUST process text-extractable PDFs at >100 pages/minute P50.
- Extraction caching MUST be correct — no redundant LLM calls when
  schema, passage, and extractor version are unchanged.

### NFR-2: Reliability

- Ingestion MUST be idempotent on the same input; re-ingesting a
  document produces no duplicates.
- The system MUST survive abrupt shutdown without corrupting the
  database or losing committed data.

### NFR-3: Portability

- Core MUST run on macOS (primary) and Linux (primary). Windows support
  is desired but best-effort.
- The data directory (Postgres + configs + extractions) MUST be
  backup/restoreable as a single portable bundle.

### NFR-4: Privacy

- No corpus data leaves the user's machine except for explicit LLM calls
  made for search query understanding, extraction, and rerank — and only
  the specific content needed for that call.
- The user MUST be able to see, via CLI or config, exactly what external
  APIs are being called and with what data scopes.

### NFR-5: Observability

- All LLM calls MUST be logged with token counts, model, cost estimate,
  and tool that invoked them.
- A `corpus-engine status` command MUST display corpus size,
  extraction counts, cache hit rates, and recent operations.

### NFR-6: Extensibility

- The core-pack API MUST be versioned. Breaking changes require a
  major-version bump and corresponding manifest compatibility gates.
- Packs MUST NOT have access to core internals beyond the declared SDK.

## Sensitive ingestion sources

This is a product-level decision that affects what the main repository
contains.

**Principle:** the core and the officially-maintained packs ship only
ingestion modules that are unambiguously appropriate to publish and
advertise. Anything in a legal, ToS, or ethical grey area is supported
architecturally (users can install packs for it) but is not included in
or advertised by the main repo.

**In the main repo (core or official `history`/`biblical-studies` packs):**

- PDF (text-extractable)
- Plain text, Markdown
- EPUB (unencrypted)
- HTML / web archives
- TEI / XML
- CSV/JSON metadata sidecars
- Markdown/plain-text exports from note apps
- Public archival formats (EAD finding aids, OSIS for biblical text,
  JATS for scientific articles)

**Not in the main repo; users install at their own discretion from
third-party packs they locate themselves:**

- DRM-protected ebook formats (Kindle, Apple Books, etc.)
- Proprietary paid-platform content (Logos resources, Accordance,
  paid journal platforms)
- Anything requiring circumvention of technical protection measures
- Scraped content from sites with ToS restrictions

The README and documentation of the main repo MUST NOT contain how-to
guidance for ingesting these sources, nor link to packs that do. The
architecture MUST accommodate them cleanly so users who choose to use
such packs have a good experience.

See [06-ingestion-modules.md](06-ingestion-modules.md) for the full
module catalog and classification.

## Success criteria

v1 is considered successful if:

1. The primary author conducts the full McClellan/Barlow research
   project end-to-end using Corpus Engine and Claude Code, producing
   publishable research artifacts (gap analyses, sentiment timelines,
   citation-grounded syntheses).
2. A second domain pack (biblical studies) is built and used in real
   work, with the core unchanged or minimally changed.
3. An external beta user installs the engine and the history pack from
   GitHub and successfully ingests and queries their own corpus within
   one working day of setup time.
4. Zero hallucinated citations are found in any produced research
   artifact (every claim traces cleanly to a source span).

## Non-goals (explicit)

- We are not building a vector database. We use pgvector.
- We are not building an LLM. We use the user's configured LLM.
- We are not building a reading UI. Users read in their existing tools.
- We are not building a team/enterprise product in v1.
- We are not building a pack marketplace in v1.
