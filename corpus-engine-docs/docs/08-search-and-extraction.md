# 08 — Search & Extraction Framework

This document details the two core primitives that most of Corpus
Engine's value rests on: hybrid search and structured extraction.

## Hybrid search

### Why hybrid

Vector search and keyword search have complementary failure modes:

- **Vector search whiffs on**: proper nouns, rare/technical terms,
  archaic spellings, specific dates, exact quotes. A search for
  "Barlow" can be swamped by semantic neighbors of the surrounding
  context.
- **Keyword search whiffs on**: conceptual queries, paraphrase,
  synonyms, cross-lingual matches. "Passages where McClellan
  expresses frustration with civilian leadership" has no reliable
  keyword.

Together they cover each other's blind spots. The hybrid layer is
therefore the default search in Corpus Engine. Pure vector and pure
keyword are available but rarely the best choice.

### Search pipeline

```
[Query + filters]
      │
      ▼
┌──────────────────────────┐
│ 1. Query understanding   │  (optional, LLM-assisted)
│    - entity resolution   │
│    - date parsing        │
│    - filter extraction   │
└──────┬───────────────────┘
       ▼
┌──────────────────────────┐
│ 2. Candidate filtering   │  SQL: apply metadata filters
│    (Postgres)            │  → pre-filtered passage ID set
└──────┬───────────────────┘
       │
  ┌────┴─────┐
  ▼          ▼
┌──────┐  ┌───────┐
│ Vec  │  │ FTS   │   parallel
│ ANN  │  │ BM25  │
└──┬───┘  └───┬───┘
   │          │
   └────┬─────┘
        ▼
┌──────────────────────────┐
│ 3. Fusion                │  RRF by default
│                          │  weighted as option
└──────┬───────────────────┘
       ▼
┌──────────────────────────┐
│ 4. Rerank (optional)     │  cross-encoder on top-N
└──────┬───────────────────┘
       ▼
┌──────────────────────────┐
│ 5. Hydrate & return      │  full metadata + snippets
└──────────────────────────┘
```

### Stage 1 — Query understanding (optional)

Off by default in v1. When enabled, an LLM call parses the query to:

- Identify entity references ("McClellan" → `person:mcclellan_gb` if
  unambiguous).
- Parse date expressions ("in July 1862" → `date_range` filter).
- Split implicit filters from the semantic core of the query.

Cached by query hash. Skipped when the agent has already structured
the query (which is the common case — Claude Code typically sends
already-parsed filters).

### Stage 2 — Candidate filtering

Pure Postgres, no LLM. Applies all declared filters as a `WHERE`
clause producing a set of candidate passage IDs. For small filtered
sets (<1000), we can skip vector ANN and score everything. For large
sets, we rely on ANN.

Key filter categories:

- Document-level: `document_type`, `author`, `date_range`, arbitrary
  metadata JSONB path matches.
- Passage-level: `passage.metadata` matches.
- Entity-level: passages mentioning specified entities (joined via
  `mentions`).

### Stage 3 — Parallel retrieval

**Vector retrieval.** pgvector HNSW index, cosine similarity. `k`
configurable; default 100 for hybrid (not the final `k` the user sees).

```sql
SELECT p.id, 1 - (pe.embedding <=> :query_vec) AS vec_score
FROM passage_embeddings pe
JOIN passages p ON p.id = pe.passage_id
WHERE pe.model = :model AND pe.model_version = :mv
  AND p.id = ANY(:candidate_ids)
ORDER BY pe.embedding <=> :query_vec
LIMIT :k_vec;
```

**Keyword retrieval.** Postgres FTS via `ts_rank_cd`:

```sql
SELECT p.id, ts_rank_cd(pf.ts, query) AS kw_score
FROM passage_fts pf, to_tsquery(:lang, :query_tsq) query
JOIN passages p ON p.id = pf.passage_id
WHERE p.id = ANY(:candidate_ids)
  AND pf.ts @@ query
ORDER BY kw_score DESC
LIMIT :k_kw;
```

Both stages run in parallel.

### Stage 4 — Fusion

**Reciprocal Rank Fusion (RRF) — default.** Parameter-free, robust
across disparate score scales.

```
score_rrf(d) = Σ_r 1 / (k + rank_r(d))
               for each ranked list r
```

With `k = 60` as the standard constant. Pros: no per-list score
normalization; no hyperparameter tuning per corpus. This is the right
default for a general-purpose product.

**Weighted sum — optional.** When agents want to tune:

```
score_w(d) = α * normalize(vec_score(d))
           + (1-α) * normalize(kw_score(d))
```

with scores min-max normalized per list. Offered when `mode:
"weighted"` is specified.

### Stage 5 — Rerank (optional but recommended)

Cross-encoder rerank of the top-N (typically 20–30) fused hits. A
cross-encoder reads query + passage together and assigns a relevance
score that's typically far more accurate than either retrieval stage
alone, at the cost of one LLM-scale call per candidate.

Options under evaluation (see [10-open-questions.md](10-open-questions.md)):

- Cohere Rerank API (hosted, fast, costs money).
- BGE-reranker-large (local, free, slower).
- Anthropic-based reranker via prompt (convenient, higher latency).

Reranking is opt-in per search. For research-grade precision it's
almost always worth it; for interactive search it may not be.

### Stage 6 — Hydration

Join passage IDs with their documents and metadata, attach score
breakdowns, truncate text to a configurable snippet length (full text
available via `get_passage_context`).

### Filters — a non-negotiable first-class citizen

Vector search alone is a toy. Vector search + rich metadata filters is
a research tool. Filters must include at minimum:

- Date ranges (fuzzy-aware)
- Author / sender / recipient
- Document type
- Corpus subset / collection tag
- Register (public/private if tagged by pack)
- Entity presence (including "and" / "or" / "not" over multiple entities)
- Language
- Any JSONB metadata path

The query planner should push filters down before ANN where possible
to narrow the candidate set.

### Performance targets

| Corpus size   | P50 latency | P95 latency |
|---------------|-------------|-------------|
| 10k passages  | <200ms      | <500ms      |
| 100k passages | <800ms      | <2s         |
| 1M passages   | <2s         | <5s         |

With rerank enabled, add the rerank latency (typically 300–800ms per
20 candidates with a hosted reranker; longer for local).

### Evaluation

Every search release should be regression-tested against a fixed set
of query/relevance pairs drawn from the McClellan corpus:

- Exact-phrase queries (should surface the specific passages that
  contain them).
- Semantic queries (should surface passages covering the concept
  without the specific words).
- Entity-focused queries (should handle aliases correctly).
- Filter-heavy queries (should return only within-scope passages).
- Empty-result queries (should return empty cleanly, not wild
  irrelevant results).

Metrics: recall@10, MRR, nDCG@10 against labeled relevance.

---

## Extraction framework

### Purpose

The `extract(passage_ids, schema)` primitive turns unstructured prose
into structured, queryable data with source attribution. It's the
lever that makes research-grade capabilities — gap detection,
sentiment timelines, contradiction detection — expressible as simple
compositions of tools.

### Schema format

Extraction schemas are YAML files with the following shape:

```yaml
id: epistolary_references
version: 2
description: |
  Extract references within a letter to other letters (prior, received,
  enclosed, or concerning third parties).
owner: pack:history

# The LLM produces a list of records of these types.
record_types:
  - id: epistolary_reference
    description: A reference to another letter
    fields:
      reference_type:
        type: enum
        values: [prior_letter, received_letter, enclosure, mentioned_letter, third_party_letter]
        required: true
      referenced_party_surface:
        type: string
        description: "The name as it appears in the text"
        required: true
      referenced_party_entity_id:
        type: entity_ref
        entity_type: person
        description: "Resolved entity; leave null if ambiguous"
        required: false
      referenced_date:
        type: fuzzy_date
        description: "The date of the referenced letter, as discernible"
        required: false
      content_hint:
        type: string
        description: "Brief phrase describing what the referenced letter was about"
        required: false
      evidence:
        type: evidence_span
        description: "Exact substring of the passage that establishes this reference"
        required: true
      confidence:
        type: number
        range: [0, 1]
        required: true

# Prompt template Jinja-rendered with {passage_text, entity_hints, …}.
prompt: |
  You are extracting references to other letters from an excerpt of
  historical correspondence.

  For each reference you find, produce a record of type
  epistolary_reference.

  Rules:
  - Only extract references that are explicit or nearly so.
  - The `evidence` field MUST be a verbatim substring of the passage.
  - If the referenced letter's date is mentioned ("yours of the 15th"),
    extract it; if it's implied but uncertain ("your last"), leave
    referenced_date null.
  - Be conservative with confidence: 0.9+ only for explicit references.

  Passage:
  """
  {{ passage_text }}
  """

  Entity hints (known correspondents):
  {{ entity_hints }}

  Output JSON matching the schema.

# Validation rules beyond JSON Schema
validation:
  - evidence_must_be_substring: true
  - evidence_max_length: 200
```

### Field types

The schema language supports these semantic types beyond plain
JSON-Schema primitives:

- `evidence_span` — must be a verbatim substring of the passage;
  the framework converts it to byte offsets post-extraction.
- `entity_ref` — optionally typed; the framework attempts to resolve
  and attaches the resolved entity ID; unresolved references are kept
  with the surface form.
- `fuzzy_date` — parsed into `{start, end, precision}`.
- `date_range` — two dates.
- `enum` — constrained value list.
- `number` with `range` — numeric bounds.
- Standard JSON-Schema types (string, number, integer, boolean,
  array, object) otherwise.

### Invocation

```python
extractions = core.extract(
    passage_ids=[...],
    schema="epistolary_references:v2",
    options={
        "llm_model": None,            # use core default
        "force_refresh": False,
        "concurrency": 8,
        "entity_hints_source": "document_correspondents",
    },
)
```

Or from the MCP tool `extract` (see [05-mcp-spec.md](05-mcp-spec.md)).

### Execution

1. **Cache check.** Key on
   `sha256(passage_id, schema_id, schema_version, extractor_version, llm_model)`.
   Hits return immediately.
2. **Prompt assembly.** Render the Jinja template with the passage
   text and any context hints the schema requests.
3. **LLM call.** Core's `LLMClient` with structured-output mode where
   supported.
4. **Validation.** Validate against the JSON Schema + semantic rules
   (`evidence_must_be_substring` etc.). On failure: one retry with the
   validation error appended to the prompt; then surface the failure.
5. **Post-processing.**
   - Resolve `entity_ref` fields against the entity store.
   - Convert `evidence_span` text to byte offsets.
   - Parse `fuzzy_date` strings to structured form.
6. **Write.** Insert extraction row + per-record rows in a single
   transaction. Log the LLM call.

### Versioning

Three version axes matter and are tracked independently:

- **Schema version** — bump when schema fields or semantics change.
- **Extractor version** — bump when the prompt or post-processing
  changes but the schema is unchanged.
- **LLM model** — tracked on every extraction.

Cache keys include all three. A user can re-run an extraction against
a new model or extractor version and compare results without losing
the old extractions.

### Batching and concurrency

- Passage-level parallelism with configurable concurrency.
- Per-LLM-provider rate limiting honored.
- Progress reporting: agents calling `extract` over large sets get
  back a job handle and poll for completion, or (for small batches)
  the call blocks.

### Cost visibility

Every extraction logs input/output token counts and an estimated cost.
`corpus-engine status` reports cumulative extraction cost broken down
by schema and model.

### Quality guardrails

- **Evidence-required.** Every record must include an evidence span.
  Records without one are rejected during validation.
- **Substring verification.** Evidence spans that don't appear in the
  passage are rejected. This is the primary guard against
  hallucination.
- **Confidence calibration.** Schemas can require a confidence field;
  low-confidence records are flagged but stored (callers filter).
- **Retry on invalid JSON.** Exactly one retry with the error appended.
  Repeated failures surface cleanly.

### Composing extractions

Extractions compose naturally:

```
find_passages
  → extract (claims)
  → materialize as claim_made events
  → events(aggregate=mean_stance, group_by=month)
  → chart/timeline
```

Or:

```
find_passages (letters)
  → extract (epistolary_references)
  → query_extractions filter unresolved
  → history.find_missing_letters output
```

Most high-value capabilities are search + extract + query + render.
The framework is designed so the agent can compose these without custom
plumbing for each use case.

### Non-goals

- We are not building a training-data pipeline. Extractions are for
  querying, not for fine-tuning.
- We are not building agentic extraction. A single LLM call per
  passage per schema is the target; multi-step agentic extraction is a
  future consideration only if quality demands it.
- We are not building a rule-based extraction system. Where rules are
  appropriate, they go in pack post-processors, not the extraction
  schema itself.
