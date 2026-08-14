# 03 — Technical Architecture

## Architectural overview

Corpus Engine is a layered, single-process local application exposing its
capabilities to AI agents through MCP. The layers are:

```
                          ┌───────────────────────────────┐
  AI Agent (Claude Code,  │        MCP Client             │
  Cursor, etc.)           └──────────────┬────────────────┘
                                         │ MCP protocol
                          ┌──────────────▼────────────────┐
                          │        MCP Server             │
                          │  (exposes core + pack tools)  │
                          └──────────────┬────────────────┘
                                         │
               ┌─────────────────────────┼─────────────────────────┐
               │                         │                         │
       ┌───────▼───────┐        ┌────────▼─────────┐      ┌────────▼────────┐
       │   Core Tools  │        │    Pack Tools    │      │       CLI       │
       │ (search, get, │        │ (domain-specific │      │ (ingest, admin, │
       │  extract,     │        │  e.g. history    │      │  pack mgmt,     │
       │  events, …)   │        │  gap detection)  │      │  serve)         │
       └───────┬───────┘        └────────┬─────────┘      └────────┬────────┘
               └─────────────────┬───────┴─────────────────────────┘
                                 │
                  ┌──────────────▼───────────────┐
                  │      Core Engine Services    │
                  ├──────────────────────────────┤
                  │ • Ingestion pipeline         │
                  │ • Chunking                   │
                  │ • Extraction framework       │
                  │ • Hybrid search              │
                  │ • Entity / mention resolver  │
                  │ • Event / timeline engine    │
                  │ • Provenance store           │
                  │ • Pack loader / sandbox      │
                  │ • LLM client (pluggable)     │
                  └──────────────┬───────────────┘
                                 │
                  ┌──────────────▼───────────────┐
                  │          Storage             │
                  ├──────────────────────────────┤
                  │  Postgres (with pgvector)    │
                  │  • documents / passages      │
                  │  • entities / mentions       │
                  │  • events / relations        │
                  │  • extractions / provenance  │
                  │  • vector index (pgvector)   │
                  │  • FTS index (tsvector)      │
                  │                              │
                  │  File storage                │
                  │  • originals                 │
                  │  • parsed text cache         │
                  │  • pack installs             │
                  │  • LLM call log              │
                  └──────────────────────────────┘
```

## Component responsibilities

### Ingestion pipeline

Orchestrates the document-entry flow:

1. **Source discovery** — locates files or fetches from remote sources.
2. **Parser dispatch** — routes each document to an appropriate parser
   based on MIME type, extension, or explicit declaration. Parsers live
   in core (default formats) or in packs (domain/source-specific).
3. **Structural decomposition** — splits document into passages using
   the chunking strategy appropriate to the document type.
4. **Initial metadata extraction** — pulls whatever the parser can
   produce (title, author, dates from file metadata or content).
5. **Embedding generation** — computes vector embeddings for each
   passage using the configured embedding model.
6. **Index update** — writes document, passages, embeddings, and
   FTS tokens into Postgres.
7. **Provenance record** — writes the full ingestion trail.

Ingestion is idempotent and resumable. See
[06-ingestion-modules.md](06-ingestion-modules.md) for module-specific
detail.

### Chunking service

A pluggable component that transforms a parsed document into passages.
Core ships defaults:

- **Prose default** — sentence-boundary-aware windows (~500 tokens
  with ~50 token overlap).
- **Whole-document** — for short documents (≤ a configurable threshold).
- **Structural** — respects document structure (headings, paragraphs)
  where the parser exposes it.

Packs can register additional chunkers keyed to document types they
define.

### Extraction framework

The `extract(passage_ids, schema, options)` primitive. Flow:

1. Resolve schema (either inline JSON Schema-like definition or a
   registered named schema from a pack).
2. Compute cache key: `hash(passage_id, schema_version, extractor_version)`.
3. Return cached result if present.
4. Otherwise, construct LLM prompt from schema + passage text, call the
   configured LLM, validate output against schema.
5. Store each extracted record with its span reference.
6. Log the LLM call.

Schemas declare which fields are evidence spans, which are structural,
which are free-text, so the framework can validate that spans actually
occur in the source passage.

See [08-search-and-extraction.md](08-search-and-extraction.md).

### Hybrid search

Implements the search pipeline:

1. Parse query (optional LLM call for query understanding if enabled).
2. Apply metadata filters to candidate passage set (Postgres).
3. In parallel: vector search (pgvector HNSW), keyword search
   (Postgres FTS / tsvector).
4. Fuse results (RRF default).
5. Optional cross-encoder rerank of top-N.
6. Return hits with rich metadata and score breakdown.

See [08-search-and-extraction.md](08-search-and-extraction.md).

### Entity and mention resolver

- Stores entity records with types, canonical names, aliases.
- Resolves mentions: given a passage containing "Little Mac," match to
  the canonical `person:mcclellan_gb` entity.
- Alias resolution uses a combination of exact match, fuzzy match, and
  (where needed) embedding similarity against entity descriptions.
- Manual overrides and corrections are persisted.

### Event / timeline engine

- Stores events as first-class records with fuzzy-time support.
- Exposes timeline queries: filter, group, bin, aggregate.
- Supports multi-stream overlay for comparative timelines.

### Provenance store

Not a separate service but a cross-cutting concern. Every derived record
(mention, extraction, event, relation) has a foreign key chain back to
the passage and document. The provenance query returns this chain for
any derived record.

### Pack loader and sandbox

- Installs packs from GitHub URLs, local paths, pinned SHAs.
- Validates the manifest.
- Loads pack-declared capabilities (entity types, event types,
  extractors, MCP tools, ingestion adapters, chunkers).
- Enforces declared permissions where feasible.

See [07-pack-system.md](07-pack-system.md) for the pack architecture.

### LLM client

A thin abstraction over LLM providers. v1 targets:

- Anthropic (default, first-class).
- OpenAI-compatible APIs (secondary, via compatibility layer).
- Local models via Ollama / llama.cpp (best-effort; not blocking for v1).

The embedding model is separately configured (default:
sentence-transformers model locally or a remote embedding API — decision
pending, see [10-open-questions.md](10-open-questions.md)).

### CLI

The admin and operations surface. See [05-mcp-spec.md](05-mcp-spec.md)
§"CLI" for the command catalog.

### MCP server

Implements the MCP protocol, registers all tools (core + enabled pack
tools), and handles incoming tool invocations. Standard MCP stdio and
HTTP+SSE transports supported.

## Data flow: ingestion

```
[User]                [CLI]              [Ingestion]           [Storage]
  │                     │                     │                     │
  │  ingest ./letters   │                     │                     │
  │────────────────────▶│                     │                     │
  │                     │   scan + dispatch   │                     │
  │                     │────────────────────▶│                     │
  │                     │                     │  parse (PDF/EPUB/…) │
  │                     │                     │                     │
  │                     │                     │  chunk              │
  │                     │                     │                     │
  │                     │                     │  embed (batched)    │
  │                     │                     │                     │
  │                     │                     │  write docs +       │
  │                     │                     │  passages + vectors │
  │                     │                     │────────────────────▶│
  │                     │                     │                     │
  │                     │                     │  write provenance   │
  │                     │                     │────────────────────▶│
  │                     │  per-file status    │                     │
  │                     │◀────────────────────│                     │
  │ progress / errors   │                     │                     │
  │◀────────────────────│                     │                     │
```

## Data flow: search + extract (agent-driven)

```
[Claude Code]    [MCP Server]     [Search]      [Extraction]    [Storage]
     │                │                │               │              │
     │ find_passages  │                │               │              │
     │───────────────▶│                │               │              │
     │                │  hybrid_search │               │              │
     │                │───────────────▶│               │              │
     │                │                │  FTS + vec    │              │
     │                │                │──────────────────────────────▶│
     │                │                │  RRF + rerank │              │
     │                │  hits          │               │              │
     │                │◀───────────────│               │              │
     │  hits          │                │               │              │
     │◀───────────────│                │               │              │
     │                │                │               │              │
     │ extract_claims │                │               │              │
     │───────────────▶│                │               │              │
     │                │  extract       │               │              │
     │                │───────────────────────────────▶│              │
     │                │                │               │ cache lookup │
     │                │                │               │─────────────▶│
     │                │                │               │ miss → LLM   │
     │                │                │               │              │
     │                │                │               │ store        │
     │                │                │               │─────────────▶│
     │                │  records       │               │              │
     │                │◀───────────────────────────────│              │
     │  records       │                │               │              │
     │◀───────────────│                │               │              │
```

## Key architectural choices

### Why Postgres + pgvector (not a dedicated vector DB)

- **Unified storage.** Keeping metadata, graph, FTS, and vectors in one
  database lets queries combine all three without cross-store joins.
  Hybrid search with metadata pre-filtering is a first-class query.
- **Operational simplicity.** One database to back up, restore, tune.
- **Good enough scale.** pgvector with HNSW handles millions of vectors
  comfortably on a laptop, well beyond single-researcher corpora.
- **Graph queries.** Edges/relationships can be queried via recursive
  CTEs; Apache AGE is an option if we need fuller graph semantics.

A future v2 decision point is whether large institutional corpora
warrant a dedicated vector store; v1 does not need one.

### Why an MCP server as the product surface

- Claude Code, Cursor, and an increasing number of agents speak MCP
  natively. Building on MCP means the user's existing agent *is* the
  interface.
- Avoids the trap of building yet another research UI that will never
  be as good as an agent loop.
- Makes the engine composable with other MCPs the user has connected
  (e.g. the user's Logos MCP, filesystem MCP, web search MCPs).

### Why core is domain-agnostic

The McClellan use case is load-bearing for v1, but generality is the
long-term product bet. The core is designed so the history pack can be
factored out cleanly once the biblical-studies pack forces the right
abstractions. See [07-pack-system.md](07-pack-system.md) §"Core-pack
boundary."

### Why extraction is a first-class primitive

Once `extract(passage_ids, schema)` exists as a reliable, cached,
provenance-preserving primitive, an enormous range of research
capabilities become expressible as compositions of search + extract:

- Gap detection = extract references, query for unresolved ones.
- Sentiment timelines = extract stance claims, aggregate over time.
- Cross-reference networks = extract citations, build the graph.
- Contradiction detection = extract claims, find conflicting ones.
- Entity resolution = extract attribute-bearing mentions, cluster.

The extraction primitive is the lever that turns search into
research-grade tooling.

### Why pack distribution is via GitHub URL, not a registry

- Zero infrastructure to maintain at launch.
- No gatekeeping, which matters for niche academic domains.
- Familiar to the technical user base.
- A registry can be layered on later (even as a community-maintained
  git repo of manifests) without changing the installation model.

## Technology choices

| Concern | Choice | Rationale |
|---------|--------|-----------|
| Language | Python 3.11+ | Rich LLM/ML ecosystem; matches likely pack author stack. |
| DB | Postgres 15+ with pgvector | See above. |
| FTS | Postgres tsvector | Built-in, avoids another system; adequate for single-researcher scale. |
| Vector index | pgvector HNSW | Native to Postgres; good ANN performance. |
| MCP | Python MCP SDK (official) | Standard. |
| Packaging | uv / pip with manifest | Standard Python tooling. |
| CLI | Typer or Click | Fast dev, good UX. |
| Embedding default | TBD — see [10-open-questions.md](10-open-questions.md) | Local vs. remote tradeoff. |
| LLM default | Anthropic API | Primary test target; user-configurable. |

## Deployment model

v1 ships as a Python package installable via `pip` or `uv`. A single
`corpus-engine` CLI provides all administrative functions. A local
Postgres instance is required; the installer may optionally provision
one via Docker Compose or document manual setup.

## Failure modes and handling

- **LLM unavailable.** Extraction and query-understanding degrade
  gracefully: search still works with raw query text; extraction queues
  or returns a clear error.
- **Embedding model unavailable.** Ingestion can proceed and fall back
  to keyword-only indexing, flagging passages for embedding on next
  successful run.
- **Pack load failure.** The pack is skipped with a clear error; other
  packs and core continue.
- **Corrupt ingestion input.** Per-document isolation ensures one bad
  file doesn't abort a batch; errors are reported and ingestion resumes.
- **Database unavailable.** The engine refuses to start with a clear
  diagnostic pointing to DB config.

## Security posture (single-user v1)

- The engine runs as a local process under the user's account; there is
  no multi-tenant trust model.
- Outbound LLM API calls are the primary data-egress channel; the engine
  logs all such calls and what passages they included.
- Pack code runs with the user's local privileges. The permissions model
  is advisory — we declare what packs need, and the user approves at
  install time. True sandboxing (WASM / subprocess isolation) is a
  v2 goal; see [10-open-questions.md](10-open-questions.md).
- Secrets (LLM API keys, etc.) live in a config file outside the repo
  and in environment variables.
