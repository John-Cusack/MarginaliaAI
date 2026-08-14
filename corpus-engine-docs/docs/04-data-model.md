# 04 — Data Model & Core Schemas

This document specifies the core database schema. All tables below live in
the `core` schema unless noted. Packs may create additional tables in
their own namespaced schemas (`pack_history`, `pack_biblical`, etc.).

Conventions:

- Primary keys are UUIDs (`uuid_generate_v7()` or equivalent
  time-ordered UUIDs preferred for index locality).
- All timestamps are stored in UTC.
- JSONB used for flexible metadata with packs responsible for declaring
  their schemas.
- Text columns use UTF-8; database collation `C.UTF-8`.

## Entity-relationship overview

```
                ┌─────────────┐
                │  documents  │
                └──────┬──────┘
                       │ 1..N
                ┌──────▼──────┐
                │  passages   │◀────────────────┐
                └──────┬──────┘                 │
                       │ 1..N                   │
                ┌──────▼──────┐                 │
                │  mentions   │───────┐         │
                └─────────────┘       │         │
                                      │         │
                            N..1      │         │
                              ┌───────▼──────┐  │
                              │   entities   │  │
                              └──────────────┘  │
                                                │
                ┌─────────────┐                 │
                │   events    │─────────────────┘ (evidence passage)
                └─────────────┘
                       │
                ┌──────▼──────┐
                │ event_actors│──── N..1 ──▶ entities
                └─────────────┘

                ┌─────────────┐
                │ extractions │─── N..1 ──▶ passages
                └─────────────┘    N..1 ──▶ extraction_schemas

                ┌─────────────┐
                │    edges    │ (entity/document/event relations)
                └─────────────┘
```

## Tables — core document and passage layer

### `documents`

The canonical record of an ingested document.

```sql
CREATE TABLE core.documents (
    id              uuid PRIMARY KEY,
    title           text,
    document_type   text NOT NULL,              -- 'letter', 'book', 'article', etc.
                                                -- registered by packs; 'generic' in core
    language        text,                       -- BCP-47
    source          text NOT NULL,              -- original file path or URI
    content_hash    bytea NOT NULL,             -- sha256 of source bytes
    parser          text NOT NULL,              -- name of parser used
    parser_version  text NOT NULL,
    ingested_at     timestamptz NOT NULL DEFAULT now(),
    created_date    daterange,                  -- fuzzy date the document was authored
    created_precision text,                     -- 'day','month','season','year','decade'
    metadata        jsonb NOT NULL DEFAULT '{}'::jsonb,
                                                -- extended metadata; schema governed by
                                                -- document_type
    UNIQUE (content_hash, source)
);

CREATE INDEX documents_type_idx ON core.documents (document_type);
CREATE INDEX documents_created_idx ON core.documents USING gist (created_date);
CREATE INDEX documents_metadata_idx ON core.documents USING gin (metadata);
```

Notes:

- `created_date` is a `daterange` to honor fuzzy authorship dates.
- `metadata` holds pack-specific extended fields (e.g. for letters:
  `sender_entity_id`, `recipient_entity_id`, `place_of_writing`, archival
  references, edition info).
- Packs providing document types are expected to publish JSON Schema
  definitions for their `metadata` shape; core validates on write.

### `passages`

A passage is a chunk of a document — the unit of retrieval.

```sql
CREATE TABLE core.passages (
    id              uuid PRIMARY KEY,
    document_id     uuid NOT NULL REFERENCES core.documents(id) ON DELETE CASCADE,
    position        int  NOT NULL,              -- ordinal within document
    locator         jsonb NOT NULL,             -- {byte_start, byte_end, …} or
                                                -- structural locator (page, section, verse)
    text            text NOT NULL,
    token_count     int,
    chunker         text NOT NULL,              -- name of chunker that produced this
    chunker_version text NOT NULL,
    metadata        jsonb NOT NULL DEFAULT '{}'::jsonb,
                                                -- inherited + passage-specific
    content_hash    bytea NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (document_id, position, chunker, chunker_version)
);

CREATE INDEX passages_document_idx ON core.passages (document_id);
CREATE INDEX passages_metadata_idx ON core.passages USING gin (metadata);
```

### `passage_embeddings`

Separated from `passages` because embeddings get re-generated when the
embedding model changes, and we want to retain multiple generations
during migration.

```sql
CREATE TABLE core.passage_embeddings (
    passage_id      uuid NOT NULL REFERENCES core.passages(id) ON DELETE CASCADE,
    model           text NOT NULL,              -- e.g. 'bge-large-en-v1.5'
    model_version   text NOT NULL,
    dim             int  NOT NULL,
    embedding       vector NOT NULL,            -- pgvector
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (passage_id, model, model_version)
);

-- HNSW index created per (model, model_version) since vector dim varies.
-- Example:
-- CREATE INDEX passage_emb_bge_hnsw ON core.passage_embeddings
--   USING hnsw (embedding vector_cosine_ops)
--   WHERE model = 'bge-large-en-v1.5';
```

### `passage_fts`

Full-text search index materialized as a tsvector.

```sql
CREATE TABLE core.passage_fts (
    passage_id      uuid PRIMARY KEY REFERENCES core.passages(id) ON DELETE CASCADE,
    lang_config     regconfig NOT NULL DEFAULT 'english',
    ts              tsvector NOT NULL
);

CREATE INDEX passage_fts_ts_idx ON core.passage_fts USING gin (ts);
```

## Tables — entity layer

### `entities`

```sql
CREATE TABLE core.entities (
    id              uuid PRIMARY KEY,
    entity_type     text NOT NULL,              -- 'person', 'place', 'org', etc.
                                                -- registered by packs
    canonical_name  text NOT NULL,
    disambiguator   text,                       -- e.g. '(general)'
    attributes      jsonb NOT NULL DEFAULT '{}'::jsonb,
                                                -- type-specific attributes
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX entities_type_idx ON core.entities (entity_type);
CREATE INDEX entities_name_trgm ON core.entities USING gin (canonical_name gin_trgm_ops);
CREATE INDEX entities_attrs_idx ON core.entities USING gin (attributes);
```

### `entity_aliases`

```sql
CREATE TABLE core.entity_aliases (
    entity_id       uuid NOT NULL REFERENCES core.entities(id) ON DELETE CASCADE,
    alias           text NOT NULL,
    alias_type      text,                       -- 'nickname', 'title', 'former_name', 'code'
    PRIMARY KEY (entity_id, alias)
);

CREATE INDEX entity_aliases_alias_trgm ON core.entity_aliases USING gin (alias gin_trgm_ops);
```

### `mentions`

A mention links a passage to an entity.

```sql
CREATE TABLE core.mentions (
    id              uuid PRIMARY KEY,
    passage_id      uuid NOT NULL REFERENCES core.passages(id) ON DELETE CASCADE,
    entity_id       uuid NOT NULL REFERENCES core.entities(id) ON DELETE RESTRICT,
    span_start      int,                        -- byte offset in passage.text
    span_end        int,
    surface_form    text NOT NULL,              -- the text as it appeared
    confidence      real NOT NULL,              -- 0..1
    source          text NOT NULL,              -- 'llm_extraction','rule','manual'
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX mentions_passage_idx ON core.mentions (passage_id);
CREATE INDEX mentions_entity_idx  ON core.mentions (entity_id);
```

## Tables — event / timeline layer

### `events`

```sql
CREATE TABLE core.events (
    id              uuid PRIMARY KEY,
    event_type      text NOT NULL,              -- 'letter_sent', 'battle', 'claim_made',…
    timestamp_start timestamptz,                -- earliest possible
    timestamp_end   timestamptz,                -- latest possible
    precision       text,                       -- 'day','week','month','season','year','decade'
    location_id     uuid REFERENCES core.entities(id),
                                                -- if the location is a modeled entity
    location_text   text,                       -- free-text fallback
    source_passage_id uuid REFERENCES core.passages(id) ON DELETE SET NULL,
                                                -- evidence passage, if any
    payload         jsonb NOT NULL DEFAULT '{}'::jsonb,
                                                -- event_type-specific fields
    confidence      real NOT NULL DEFAULT 1.0,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX events_type_idx ON core.events (event_type);
CREATE INDEX events_time_idx ON core.events USING gist
    (tstzrange(timestamp_start, timestamp_end, '[]'));
CREATE INDEX events_payload_idx ON core.events USING gin (payload);
```

### `event_actors`

Events can involve multiple actors (sender + recipient; multiple
belligerents in a battle; etc.).

```sql
CREATE TABLE core.event_actors (
    event_id        uuid NOT NULL REFERENCES core.events(id) ON DELETE CASCADE,
    entity_id       uuid NOT NULL REFERENCES core.entities(id) ON DELETE RESTRICT,
    role            text NOT NULL,              -- 'sender', 'recipient', 'participant', …
    PRIMARY KEY (event_id, entity_id, role)
);

CREATE INDEX event_actors_entity_idx ON core.event_actors (entity_id);
```

## Tables — relationships

### `edges`

Generic directed edges between any two nodes (entity, document, event,
passage). Nodes are identified by `(kind, id)`.

```sql
CREATE TABLE core.edges (
    id              uuid PRIMARY KEY,
    source_kind     text NOT NULL,              -- 'entity','document','passage','event'
    source_id       uuid NOT NULL,
    target_kind     text NOT NULL,
    target_id       uuid NOT NULL,
    relation_type   text NOT NULL,              -- 'replies_to','cites','influenced_by',…
                                                -- registered by packs
    attributes      jsonb NOT NULL DEFAULT '{}'::jsonb,
    source_passage_id uuid REFERENCES core.passages(id) ON DELETE SET NULL,
    confidence      real NOT NULL DEFAULT 1.0,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX edges_source_idx ON core.edges (source_kind, source_id);
CREATE INDEX edges_target_idx ON core.edges (target_kind, target_id);
CREATE INDEX edges_relation_idx ON core.edges (relation_type);
```

## Tables — extraction framework

### `extraction_schemas`

Registered extraction schemas (domain-provided or user-defined).

```sql
CREATE TABLE core.extraction_schemas (
    id              uuid PRIMARY KEY,
    name            text NOT NULL,              -- 'epistolary_references', 'claims',…
    version         int  NOT NULL,
    owner           text NOT NULL,              -- 'core','pack:history','user:alice'
    schema          jsonb NOT NULL,             -- JSON Schema-like definition
    prompt_template text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (name, version, owner)
);
```

### `extractions`

One row per (passage × schema × extractor_version) invocation.

```sql
CREATE TABLE core.extractions (
    id              uuid PRIMARY KEY,
    passage_id      uuid NOT NULL REFERENCES core.passages(id) ON DELETE CASCADE,
    schema_id       uuid NOT NULL REFERENCES core.extraction_schemas(id),
    extractor_version text NOT NULL,
    llm_model       text NOT NULL,
    status          text NOT NULL,              -- 'pending','ok','failed'
    error           text,
    records         jsonb NOT NULL DEFAULT '[]'::jsonb,
                                                -- array of extracted objects
    llm_call_id     uuid REFERENCES core.llm_calls(id),
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (passage_id, schema_id, extractor_version)
);

CREATE INDEX extractions_schema_idx ON core.extractions (schema_id);
```

### `extraction_records`

For indexed / queryable access to individual records inside an
extraction's `records` array, we also materialize them into a flat
table.

```sql
CREATE TABLE core.extraction_records (
    id              uuid PRIMARY KEY,
    extraction_id   uuid NOT NULL REFERENCES core.extractions(id) ON DELETE CASCADE,
    passage_id      uuid NOT NULL REFERENCES core.passages(id) ON DELETE CASCADE,
    schema_id       uuid NOT NULL REFERENCES core.extraction_schemas(id),
    record_type     text NOT NULL,              -- schema-declared type of record
    data            jsonb NOT NULL,
    evidence_start  int,                        -- span within passage.text
    evidence_end    int,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX extraction_records_passage_idx ON core.extraction_records (passage_id);
CREATE INDEX extraction_records_type_idx ON core.extraction_records (record_type);
CREATE INDEX extraction_records_data_idx ON core.extraction_records USING gin (data);
```

## Tables — provenance & operations

### `llm_calls`

Every outbound LLM call is logged for auditability and cost tracking.

```sql
CREATE TABLE core.llm_calls (
    id              uuid PRIMARY KEY,
    purpose         text NOT NULL,              -- 'extraction','rerank','query_understanding',…
    caller          text NOT NULL,              -- 'core','pack:history',…
    model           text NOT NULL,
    input_tokens    int,
    output_tokens   int,
    cost_estimate   numeric(12,6),
    duration_ms     int,
    status          text NOT NULL,
    error           text,
    created_at      timestamptz NOT NULL DEFAULT now()
);
```

### `ingestion_runs`

Batch ingestion records.

```sql
CREATE TABLE core.ingestion_runs (
    id              uuid PRIMARY KEY,
    started_at      timestamptz NOT NULL DEFAULT now(),
    completed_at    timestamptz,
    source_spec     jsonb NOT NULL,             -- what was requested
    status          text NOT NULL,              -- 'running','ok','failed','partial'
    stats           jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE core.ingestion_items (
    id              uuid PRIMARY KEY,
    run_id          uuid NOT NULL REFERENCES core.ingestion_runs(id) ON DELETE CASCADE,
    source_ref      text NOT NULL,              -- file path / URI
    document_id     uuid REFERENCES core.documents(id),
    status          text NOT NULL,
    error           text,
    duration_ms     int,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ingestion_items_run_idx ON core.ingestion_items (run_id);
```

### `installed_packs`

Tracks installed packs.

```sql
CREATE TABLE core.installed_packs (
    id              text PRIMARY KEY,           -- pack name
    version         text NOT NULL,
    source_url      text NOT NULL,
    source_ref      text NOT NULL,              -- commit SHA
    installed_at    timestamptz NOT NULL DEFAULT now(),
    enabled         boolean NOT NULL DEFAULT true,
    manifest        jsonb NOT NULL,
    permissions_granted jsonb NOT NULL DEFAULT '{}'::jsonb
);
```

## Type registries

Registered types (document_type, entity_type, event_type, relation_type,
record_type) are logical — they are validated against a registry held
in memory by the pack loader, not against a DB-enforced enum, because
types come and go as packs are enabled/disabled.

The pack loader maintains:

```
type_registry = {
  "document_type": { "letter": {pack:"history", schema:…}, … },
  "entity_type":   { "person": {pack:"core"}, "battle": {pack:"history"}, … },
  "event_type":    { "letter_sent": {…}, "battle_occurred": {…}, … },
  "relation_type": { "replies_to": {…}, "cites": {…}, … },
  "record_type":   { "epistolary_reference": {…}, … },
}
```

Writes validate the type against the registry. Unknown types are
rejected with a clear error ("pack:history not enabled; 'letter' type
unknown").

## Fuzzy-time conventions

For fuzzy dates, always store **both endpoints and precision**:

| Human input       | `timestamp_start` | `timestamp_end`   | `precision` |
|-------------------|-------------------|-------------------|-------------|
| July 22 1862      | 1862-07-22 00:00  | 1862-07-22 23:59  | day         |
| Late July 1862    | 1862-07-22 00:00  | 1862-07-31 23:59  | week        |
| Summer 1862       | 1862-06-01 00:00  | 1862-08-31 23:59  | season      |
| 1862              | 1862-01-01 00:00  | 1862-12-31 23:59  | year        |
| Civil War era     | 1861-01-01 00:00  | 1865-12-31 23:59  | decade      |

Clients rendering dates consult `precision` to display honestly
("summer 1862" rather than "1862-06-01").

## Migration and versioning

- Schema migrations via a standard tool (Alembic for Python).
- Each core release carries a `core_api_version`; packs declare
  compatibility in their manifest.
- Breaking schema changes bump the core major version.

## Non-goals of the data model

- We do not model fine-grained *editorial* structure (TEI-style
  encoding) in core. Packs may introduce richer models if needed.
- We do not attempt to be a universal knowledge graph. The `edges`
  table is intentionally simple; domain-specific knowledge graphs
  belong in pack-owned schemas.
- We do not store embeddings outside passages in v1. Entity-level and
  document-level embeddings are a v2 consideration.
