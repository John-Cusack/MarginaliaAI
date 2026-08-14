# 05 — MCP Tool Specification

This document defines the MCP tool surface exposed by Corpus Engine.
The surface is divided into:

- **Core tools** — always available; domain-agnostic.
- **Pack tools** — registered by installed packs; domain-specific.

The split matters. Core tools are the stable primitives that pack
tools compose with. Pack tools are where domain expertise lives.

## General conventions

- All tool inputs validated against JSON Schema on invocation.
- All tool outputs are JSON; human-readable formatting is the agent's job.
- Every tool returns structured objects — never pre-formatted prose.
- IDs are UUIDs (strings in JSON).
- Dates accept ISO 8601 strings or structured fuzzy-date objects:
  `{"start": "1862-07-01", "end": "1862-08-31", "precision": "season"}`.
- Errors return `{"error": {"code": "...", "message": "...", "details": …}}`.

## Core tools — search & retrieval

### `find_passages`

Hybrid search over the passage layer. The workhorse tool.

**Input:**

```json
{
  "query": "string - natural language or keyword",
  "filters": {
    "document_type": ["letter"],
    "author_entity_id": "uuid | null",
    "recipient_entity_id": "uuid | null",
    "date_range": {"start": "1862-01-01", "end": "1862-12-31"},
    "mentions_entity_ids": ["uuid", "..."],
    "metadata": { /* document_type-specific fields */ },
    "corpus_tags": ["mcclellan_papers"]
  },
  "k": 20,
  "hybrid": {
    "mode": "rrf | weighted | vector_only | keyword_only",
    "alpha": 0.5,           // weighted mode only
    "rerank": true
  }
}
```

**Output:**

```json
{
  "hits": [
    {
      "passage_id": "uuid",
      "document_id": "uuid",
      "score": 0.87,
      "score_breakdown": {"vector": 0.82, "keyword": 0.91, "rerank": 0.89},
      "text": "...",
      "metadata": {
        "document_type": "letter",
        "sender": "G.B. McClellan",
        "recipient": "S.L.M. Barlow",
        "date": "1862-07-30",
        "date_precision": "day",
        "source_edition": "Sears 1989",
        "archive_ref": "LOC-MCC-B12-F4"
      },
      "locator": {"byte_start": 1024, "byte_end": 1486},
      "context_available": true
    }
  ],
  "total_candidates": 342,
  "applied_filters": {…}
}
```

### `get_document`

Fetch a full document by ID.

**Input:**

```json
{ "document_id": "uuid", "include_full_text": true }
```

**Output:**

```json
{
  "id": "uuid",
  "title": "…",
  "document_type": "letter",
  "metadata": {…},
  "passages": [ {"id": "uuid", "position": 0, "text": "…"}, … ],
  "full_text": "…"            // only if requested
}
```

### `get_passage_context`

Expand around a passage with N passages before/after.

**Input:**

```json
{ "passage_id": "uuid", "before": 2, "after": 2 }
```

**Output:**

```json
{
  "target": {"passage_id": "uuid", "text": "…"},
  "before": [ {"passage_id": "uuid", "text": "…"}, … ],
  "after":  [ {"passage_id": "uuid", "text": "…"}, … ],
  "document_id": "uuid"
}
```

### `similar_to`

"More like this" — vector similarity to a known passage.

**Input:**

```json
{
  "passage_id": "uuid",
  "k": 20,
  "filters": {…}
}
```

**Output:** same shape as `find_passages`.

## Core tools — entities

### `resolve_entity`

Look up or search entities by name, alias, or attributes.

**Input:**

```json
{
  "query": "McClellan",
  "entity_type": "person | null",
  "disambiguator": "general | null",
  "k": 5
}
```

**Output:**

```json
{
  "candidates": [
    {
      "entity_id": "uuid",
      "canonical_name": "George B. McClellan",
      "entity_type": "person",
      "disambiguator": "general",
      "aliases": ["Little Mac", "G.B.M."],
      "attributes": {…},
      "match_score": 0.95
    }
  ]
}
```

### `get_entity`

Fetch a full entity record including aliases and attributes.

### `find_mentions`

All passages mentioning a given entity, with filters.

**Input:**

```json
{
  "entity_id": "uuid",
  "filters": { "date_range": {…}, "document_type": [...] },
  "k": 100
}
```

**Output:** list of `{passage_id, document_id, span, surface_form,
confidence, metadata}`.

## Core tools — events & timelines

### `events`

Query the event store.

**Input:**

```json
{
  "filters": {
    "event_types": ["letter_sent"],
    "actor_entity_ids": ["uuid"],
    "date_range": {…},
    "location_id": "uuid | null",
    "payload": { /* event-type-specific */ }
  },
  "group_by": "month | week | day | event_type | actor",
  "aggregate": "count | weighted_mean | …",
  "aggregate_field": "payload.stance",
  "k": 1000
}
```

**Output:**

```json
{
  "events": [ {"id": "uuid", "event_type": "…", "timestamp_start": "…", "payload": {…}}, … ],
  "aggregates": [ {"bucket": "1862-07", "count": 12, "weighted_mean_stance": -0.3}, … ]
}
```

### `timeline_compare`

Overlay multiple event streams on a shared time axis.

**Input:**

```json
{
  "streams": [
    {"name": "McClellan→Barlow", "filters": {…}},
    {"name": "battles",          "filters": {…}}
  ],
  "time_bin": "week | day | month"
}
```

**Output:** aligned per-bucket counts/aggregates per stream.

## Core tools — extraction

### `extract`

Run a registered or ad-hoc extraction schema against passages.

**Input:**

```json
{
  "passage_ids": ["uuid", "..."],          // or
  "passage_filter": { /* same as find_passages filters */ },
  "schema": "name:version | inline schema object",
  "options": {
    "force_refresh": false,                 // bypass cache
    "llm_model": "claude-opus-4-7 | null",  // override default
    "concurrency": 8
  }
}
```

**Output:**

```json
{
  "extractions": [
    {
      "passage_id": "uuid",
      "status": "ok",
      "records": [
        {
          "record_type": "epistolary_reference",
          "data": {
            "reference_type": "prior_letter",
            "referenced_party_entity_id": "uuid",
            "referenced_date": {"start": "1862-07-22", "end": "1862-07-22", "precision": "day"},
            "confidence": 0.9
          },
          "evidence_start": 122,
          "evidence_end": 164
        }
      ],
      "from_cache": false,
      "llm_call_id": "uuid"
    }
  ]
}
```

### `list_extraction_schemas`

Discovery tool for the agent: what schemas are available?

**Output:**

```json
{
  "schemas": [
    {
      "name": "epistolary_references",
      "version": 2,
      "owner": "pack:history",
      "description": "Extract references to prior letters, received letters, enclosures",
      "record_types": ["epistolary_reference"],
      "schema": {…}
    }
  ]
}
```

### `query_extractions`

Retrieve previously-extracted records without re-running.

**Input:**

```json
{
  "record_type": "epistolary_reference",
  "passage_filter": {…},
  "data_filter": { "reference_type": "prior_letter" },
  "k": 500
}
```

**Output:** list of extraction records with their passage refs.

## Core tools — provenance

### `provenance_of`

Given any derived ID (extraction record, mention, event), return the
chain back to source.

**Input:**

```json
{ "kind": "extraction_record | mention | event", "id": "uuid" }
```

**Output:**

```json
{
  "passage": {"id": "uuid", "text": "…", "locator": {…}},
  "document": {"id": "uuid", "title": "…", "source": "…"},
  "evidence_span": {"start": 122, "end": 164, "text": "…"},
  "extraction": {"schema": "epistolary_references:v2", "extractor_version": "2025-10-15", "llm_model": "claude-opus-4-7"}
}
```

## Core tools — corpus introspection

### `corpus_stats`

Shape and coverage of the corpus, to help the agent plan.

**Input:**

```json
{ "filters": {…} }
```

**Output:**

```json
{
  "document_count": 847,
  "passage_count": 3204,
  "by_document_type": { "letter": 820, "memo": 27 },
  "by_author": { "McClellan": 612, "Barlow": 183, … },
  "date_coverage": {"min": "1861-03-08", "max": "1865-10-12"},
  "date_histogram": [ {"bucket": "1862-01", "count": 23}, … ],
  "languages": {"en": 847}
}
```

### `list_entity_types` / `list_event_types` / `list_relation_types`

Discovery tools; return the registered types with their owning pack
and schema.

## Core tools — administration (restricted)

These are available to the CLI and (optionally) to the agent depending
on config. They modify state.

### `upsert_entity`

Create or update an entity record. Used to resolve mentions manually or
to build prosopographies.

### `upsert_event`

Create an event record (used when the agent derives events from
extractions — though preferred path is extraction → post-processor →
event).

### `upsert_edge`

Create a relationship between nodes.

### `annotate_document` / `annotate_passage`

Attach user-supplied metadata corrections.

These write-tools require explicit capability grant in the MCP server
config; by default they are read-only-safe.

## Pack tool conventions

Pack tools follow the same conventions as core tools. The pack manifest
declares them. At MCP registration time, they appear in the server's
tool list with a name prefix identifying their pack (or a bare name if
unambiguous and allowed by config).

Example pack tools (from the `history` pack, for illustration):

### `history.find_missing_letters`

**Input:**

```json
{
  "correspondent_a_entity_id": "uuid",
  "correspondent_b_entity_id": "uuid",
  "date_range": {…},
  "method": "referenced | cadence | content_inference | all",
  "min_confidence": 0.6
}
```

**Output:**

```json
{
  "candidates": [
    {
      "method": "referenced",
      "expected_sender_entity_id": "uuid",
      "expected_recipient_entity_id": "uuid",
      "expected_date": {"start": "1862-07-22", "end": "1862-07-22", "precision": "day"},
      "confidence": 0.9,
      "evidence": {
        "passage_id": "uuid",
        "span_text": "yours of the 22nd",
        "source_letter_document_id": "uuid"
      },
      "content_hints": ["political situation", "Seymour's chances"],
      "likely_archive_locations": ["LOC McClellan Papers, Series 1"]
    }
  ],
  "summary": {
    "total_candidates": 14,
    "by_method": { "referenced": 6, "cadence": 5, "content_inference": 3 }
  }
}
```

### `history.correspondence_cadence`

Returns the density timeline and flagged anomalies between two
correspondents.

### `history.timeline_view`

A timeline-specialized wrapper combining event query + rendering hints.

## CLI surface (non-MCP)

For completeness, the CLI commands pack authors and ops may use:

```
corpus-engine init PATH
corpus-engine ingest SOURCES... [--pack NAME] [--metadata FILE]
corpus-engine reindex [--embeddings] [--fts]
corpus-engine search QUERY [--filters JSON] [--json]
corpus-engine extract --schema NAME:VER --filter JSON
corpus-engine pack install URL [--ref TAG_OR_SHA]
corpus-engine pack uninstall NAME
corpus-engine pack list
corpus-engine pack enable NAME
corpus-engine pack disable NAME
corpus-engine pack audit NAME
corpus-engine pack init DIR                   # scaffold a new pack
corpus-engine pack test PATH                  # run a pack's tests
corpus-engine serve [--mcp] [--port N]
corpus-engine backup PATH
corpus-engine restore PATH
corpus-engine status
```

## Tool design principles (non-negotiable)

1. **Structured in, structured out.** No natural-language parsing inside
   tool implementations; no prose-formatted output. The agent composes
   and formats.
2. **Composable.** Tools are designed so the agent chains them
   (search → extract → events → timeline). Avoid tools that try to do
   multiple steps internally when chaining would be clearer.
3. **Every hit carries provenance.** Anything pointing at content
   includes the passage/document IDs needed to look it up.
4. **Fail informatively.** Errors include a code, a human message, and
   actionable details. The agent needs enough context to decide whether
   to retry, adapt, or surface to the user.
5. **Respect cost.** Tools that invoke LLMs (extraction, rerank, query
   understanding) must be cache-aware and honor a `force_refresh`
   override only when asked.
6. **No pre-baked "workflows" in v1.** Resist adding tools like
   `research_report()` that chain multiple primitives internally. The
   agent is the orchestrator; core stays primitive.
