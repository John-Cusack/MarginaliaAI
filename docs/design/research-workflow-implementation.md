# Implementation Guide: Research-Workflow Gaps

**Companion to:** `research-workflow-gaps.md` (design/RFC)
**Status:** Ready for P0; P1–P3 ready pending spikes S1–S2

---

## How to use this document

The design doc argues *what* to build and *why*. This one says *how*, in the order
things must happen, with the seams and failure modes called out.

**Scope.** P0–P3 are specified to implementable detail. P4 is specified to the level
of schema and decisions. P5 is an outline only — several of its choices depend on
spike outcomes and on what P0–P3 teach us. Writing P5 in detail now would be fiction.

**Conventions in this repo** (verified, not assumed):

| Thing | Value |
|---|---|
| Migrations | `packages/core/src/research_engine/adapters/storage/postgres/migrations/versions/NNN_name.py` |
| Next revision | `003` (`002_edge_dedup` is head) |
| Run migrations | `make migrate` (→ `uv run alembic -c packages/core/alembic.ini upgrade head`) |
| Unit tests | `make test` (→ `uv run pytest tests/unit/ -v`) |
| Markers | `unit` (no I/O), `integration` (real Postgres), `contract` (SDK) |
| asyncio | `asyncio_mode = auto` — no `@pytest.mark.asyncio` needed |
| Lint | ruff, line-length 100, `TCH` rules on (type-only imports must be guarded) |

**Ground rules.**

1. Every phase ends green on `make test` plus its own new tests.
2. No phase may leave the corpus in a state that requires a re-ingest to recover.
   P1 is the exception and says so explicitly.
3. Schema changes ship as one migration per work item, never batched — they need to
   be revertible independently.

---

## Spikes — do these first

Four assumptions carry the risk. None takes more than a few days, and two of them
gate design decisions in P1 and P2.

### S1 — Canonical-text stability *(gates P1)*

**Question:** is `DoclingModule` output stable enough to be a permanent addressing
substrate?

**Do:** parse the same 10 PDFs twice with the pinned docling version; then bump
docling one minor version and re-parse. Diff `export_to_markdown()` output.

**Decide:** if output is stable across versions, offsets can be recomputed on demand
and `document_texts` is a cache. If it drifts — the likely outcome — then the stored
text is the *source of truth*, `parser_version` becomes part of the anchoring
contract, and re-parsing a document is a re-anchoring event. **Assume drift until
proven otherwise**; the schema in P1-2 already carries `parser_version` for this
reason.

### S2 — pgvector index with heterogeneous dimensions *(gates P2)*

**Question:** how do we index vectors while keeping multiple embedding models
coexisting?

**Correction to the design doc.** §2.3 recommended partitioning `passage_embeddings`
by `(model, model_version)` with each partition typed to its own dimension. **That
does not work.** Postgres partitions inherit the parent's column types exactly; a
partition cannot narrow `vector` to `vector(1024)` while a sibling uses
`vector(3072)`. Partitioning buys operational conveniences (cheap `DROP` of a
model's vectors) but not heterogeneous dimensions.

**Do:** on a realistic corpus, benchmark two designs, and separately measure filtered
recall for each.

- **Design A — one typed table per dimension.** `passage_embeddings_1024`,
  `passage_embeddings_3072`, each `vector(N) NOT NULL` with its own HNSW index; the
  repo dispatches on the active model's dim. Certain to work. New dimension = new
  table + migration.
- **Design B — single table, partial expression index.**
  `CREATE INDEX … USING hnsw ((embedding::vector(1024)) vector_cosine_ops) WHERE model = 'X'`,
  with queries using the identical cast so the planner matches the expression.
  Lighter if it works; fragile under refactor, and needs verifying that HNSW accepts
  a cast expression and that the planner reliably picks it.

**Decide:** default to **Design A** unless B measurably wins. A is boring and
certainly correct, and this is infrastructure that everything else sits on.

**Also measure — this is the part that matters more than the index choice:** recall
under narrow filter extensions. `vector_search` applies `passage_id = ANY(...)` as a
`WHERE` clause. Under a sequential scan that is a true pre-filter and is correct.
Under HNSW it becomes a **post-filter**: the index returns the k globally nearest,
the filter discards most, and a narrow filter can yield 3 rows where it should yield
100. Test `hnsw.iterative_scan = relaxed_order` (pgvector ≥ 0.8). If that does not
recover recall, P2 must add adaptive-k widening before the index ships.

### S3 — CSL coverage for the real corpus *(gates P3)*

**Do:** hand-map 10 documents spanning `acad`, `books`, `logos`, and `kindle` onto
the P3-1 column set. Record every field that has nowhere to go.

**Decide:** whether `csl_extra` absorbs the tail, or whether a column is missing from
the core set. Cheap now, a migration later.

### S4 — Claim-extraction quality *(gates P5 §7)*

**Do:** run `packages/plugins/history/schemas/extraction_schemas/claims.yaml` over
~300 passages of real corpus. Inspect stance and topic by hand.

**Decide:** if stance is not reliable enough to cluster on, §7 is a research problem
wearing an engineering costume. Scope it down to hand-asserted
`supports`/`contradicts` edges and drop the automated synthesis layer.

---

## P0 — Correctness (≈1 week)

Three bugs. Two of them silently corrupt research results, which is why they precede
all feature work. No schema changes; no migration.

### P0-1 — Make ignored filters impossible

**Problem.** `filter_candidate_ids`
(`adapters/storage/postgres/repositories/passages.py:235-295`) reads
`document_types`, `date_range_start`, `date_range_end`, `mentions_entity_ids`,
`metadata`, `extensions`, and `extension_logic`. It silently ignores
`author_entity_id`, `recipient_entity_id`, and `language`, all three of which
`SearchFilters` accepts (`domain/passages.py:82-88`). `hybrid.py:47` then reports
`applied_filters = filter_dict`, asserting filters that never ran.

**The design.** Do not add parallel bookkeeping of "which filters were applied."
Instead make silent-ignore structurally impossible: **raise on any filter key the
repository does not implement.** Then `applied_filters = filter_dict` becomes true by
construction, because an unhandled key would have raised. One invariant replaces two
things that can drift.

```python
# domain/errors.py
class UnsupportedFilterError(ValueError):
    """A filter key reached the repository that it does not implement."""

# repositories/passages.py
_SUPPORTED_FILTERS = frozenset({
    "document_types", "date_range_start", "date_range_end",
    "mentions_entity_ids", "metadata", "extensions", "extension_logic",
    "author_entity_id", "recipient_entity_id", "language",
})

async def filter_candidate_ids(self, filters, filter_extensions=None):
    if unknown := set(filters) - _SUPPORTED_FILTERS:
        raise UnsupportedFilterError(f"unsupported filter keys: {sorted(unknown)}")
    ...
```

Every key in `_SUPPORTED_FILTERS` must then have a real branch. Adding a field to
`SearchFilters` without a branch now fails loudly in tests rather than lying at
runtime.

**Implement the three missing branches:**

- `language` — join `documents`, `WHERE documents.language = :lang`. Trivial.
- `author_entity_id` / `recipient_entity_id` — no ingest-time author→entity link
  exists yet (P3-1 creates it). Interim: resolve the entity to its canonical name plus
  aliases via `EntityRepo.get_aliases`, then match against
  `documents.metadata->>'author'`. This is honest if documented: it is a name match,
  not an identity match. Log at `warning` with a pointer to P3-6, which replaces it
  with the real `bib_contributors.entity_id` join.

**Files:** `repositories/passages.py`, `domain/errors.py`, `services/search/hybrid.py`
(only to delete the now-redundant comment; the `applied_filters` line becomes correct
without change).

**Tests** (`tests/unit/` — needs a repo fake or `integration` marker for the SQL):
- unknown key raises `UnsupportedFilterError`
- every field of `SearchFilters` appears in `_SUPPORTED_FILTERS` — a reflection test
  over `SearchFilters.model_fields`, which is what stops this regressing
- `language` narrows results
- author name-match returns documents by that author and excludes others

**Acceptance:** a `SearchFilters` field with no repository branch fails `make test`.

### P0-2 — Per-language text search

**Problem.** `"english"` is literal at `services/search/hybrid.py:73,90` and
`services/ingestion/orchestrator.py:173,245`, although `passage_fts.lang_config` is a
`regconfig` column (`001_initial.py:76-81`) and `documents.language` exists. The
embedding model is `BAAI/bge-m3` — multilingual — so vector recall is fine while
keyword recall is degraded by English stemming, and RRF then fuses a good ranked list
with a bad one. The `logos` pack ships non-English material today.

**Index side.** Map `documents.language` (ISO 639-1) to a PG `regconfig`:

```python
# services/search/langconfig.py
_ISO_TO_PG = {"en": "english", "de": "german", "fr": "french", "es": "spanish",
              "it": "italian", "nl": "dutch", "pt": "portuguese", "ru": "russian",
              "sv": "swedish", "no": "norwegian", "da": "danish", "fi": "finnish"}

def pg_config(iso: str | None) -> str:
    return _ISO_TO_PG.get((iso or "").lower()[:2], "simple")
```

Default to `simple`, never `english`. `simple` does no stemming, which degrades
*gracefully* for an unknown language; `english` degrades *wrongly*. Pass the resolved
config at `orchestrator.py:173,245`.

**Search side — and here is the trap.** The row already carries its config, so this
looks correct:

```sql
-- DO NOT SHIP THIS
SELECT pf.passage_id, ts_rank_cd(pf.ts, plainto_tsquery(pf.lang_config, :query))
FROM core.passage_fts pf
WHERE pf.ts @@ plainto_tsquery(pf.lang_config, :query)
```

It is correct and it **destroys the GIN index**: the tsquery varies per row, so
`passage_fts_ts_idx` cannot be used and every search becomes a sequential scan over
the FTS table.

Ship the index-using form instead — one branch per distinct config, unioned:

```sql
SELECT pf.passage_id, ts_rank_cd(pf.ts, q.tsq) AS kw_score
FROM core.passage_fts pf, plainto_tsquery('english', :query) AS q(tsq)
WHERE pf.lang_config = 'english' AND pf.ts @@ q.tsq
UNION ALL
SELECT pf.passage_id, ts_rank_cd(pf.ts, q.tsq)
FROM core.passage_fts pf, plainto_tsquery('german', :query) AS q(tsq)
WHERE pf.lang_config = 'german' AND pf.ts @@ q.tsq
ORDER BY kw_score DESC LIMIT :k
```

Build the branches from `SELECT DISTINCT lang_config FROM core.passage_fts`, cached
with a short TTL and invalidated on ingest. In practice this is two or three branches.
Honour an explicit `filters.language` by restricting to that one branch.

**Also fix `index_fts`** (`repositories/passages.py:203-214`): the `ON CONFLICT DO
UPDATE` sets `ts` but not `lang_config`, so re-indexing under a new language leaves
the column disagreeing with the vector it describes. Add `lang_config = EXCLUDED.lang_config`.

**Tests:** German text indexed under `german` matches a German stem and does not match
under `english`; unknown language lands in `simple`; `EXPLAIN` on the union form shows
a bitmap index scan (`integration` marker).

### P0-3 — Cost visibility

`llm_calls.cost_estimate` is written faithfully and never read. Both adapters already
receive `llm_calls_repo` (`composition.py:150-163`), so the seam exists.

- `llm_usage` MCP tool + CLI report: group by `purpose`, `caller`, `model` over a time
  window.
- `BudgetGuard`, an `LLMPort` implementation wrapping the real adapter, refusing calls
  once configured spend is exceeded. Wire in `composition.py` behind a setting.
  Corpus-wide extraction is exactly the operation that produces a surprising bill.

---

## P1 — The anchoring foundation (3–4 weeks)

**This is the phase that blocks five features and the one people will want to skip.**
Built on today's locators, quote verification and pin-cites would be built on
`byte_start: 0`.

**This phase requires a re-chunk of the existing corpus.** It is the one exception to
ground rule 2. P1-5 makes that non-destructive; do not ship P1-1..P1-4 without it.

### P1-1 — Why the current locators are worthless

`ProseWindowChunker._make_draft` (`chunking/prose_window.py:66-76`):

```python
locator={"byte_start": 0, "byte_end": len(text.encode())}
```

`byte_start` is always `0`; `byte_end` is the length of the *chunk*, not a document
offset. `prose_window` is the `default_chunker` for `books`, `kindle_book`, and
`ycl_book` — most of the corpus. `FixedWindowChunker` computes real offsets
(`fixed_window.py:33-45`) but `.strip()`s the text afterwards, so its offsets drift
from its text.

`tests/unit/services/test_chunking.py` contains **no locator assertion at all**. That
absence is why this survived, and P1-3's invariant test is the fix.

### P1-2 — Canonical text

```sql
-- 003_document_texts.py
CREATE TABLE core.document_texts (
  document_id          uuid PRIMARY KEY REFERENCES core.documents(id) ON DELETE CASCADE,
  text                 text NOT NULL,
  normalized_text      text NOT NULL,
  normalization_version text NOT NULL,
  parser               text NOT NULL,
  parser_version       text NOT NULL
);
CREATE INDEX document_texts_norm_trgm
  ON core.document_texts USING gin (normalized_text gin_trgm_ops);
```

Separate from `documents` because `documents` is on the search hydration hot path and
should not drag a megabyte per row. `parser`/`parser_version` are recorded per S1: if
docling output drifts between versions, this text — not the file — is the addressing
substrate.

`normalized_text` and the trigram index exist for P3-5 (quote verification).
`normalization_version` lets normalization evolve without invalidating stored offsets,
since raw `text` is what offsets address.

**Storage:** ~1 MB per 500-page book; 10k documents ≈ 10 GB pre-TOAST. Fine for
single-user Postgres.

### P1-3 — The chunker contract

`PassageDraft` gains required `char_start: int` and `char_end: int`. The `locator`
JSON stays for type-specific extras (page, verse, timecode) per §3.3, but offsets are
promoted to real indexed columns.

**The invariant, which is the whole point:**

> For every chunker, every input text, and every emitted draft:
> `draft.text == text[draft.char_start:draft.char_end]`

Enforce it as a property test over all registered chunkers, including plugin-supplied
ones via the `contract` marker. It mechanically forbids both existing bugs:
`prose_window`'s `" ".join(...)` (which alters text) and `fixed_window`'s `.strip()`.

**Rewriting `prose_window`.** Work in offsets, slice once at the end:

```python
def _sentence_spans(text: str) -> list[tuple[int, int]]:
    spans, start = [], 0
    for m in _SENT_BOUNDARY.finditer(text):
        spans.append((start, m.start()))
        start = m.end()
    if start < len(text):
        spans.append((start, len(text)))
    return spans
```

Window over spans accumulating the token estimate; emit each chunk as
`text[first_start:last_end]`. Slicing from the first sentence's start to the last
sentence's end **includes the inter-sentence whitespace**, so original paragraph
structure survives — the whitespace-destruction bug disappears as a side effect of
satisfying the invariant.

Output text changes, so bump `ProseWindowChunker.version` to `"2.0"`. The existing
`(document_id, position, chunker, chunker_version)` unique constraint already permits
old and new passages to coexist, which P1-5 depends on.

Apply the same treatment to `fixed_window` (drop the `.strip()`; adjust the span
instead), `structural`, and `whole_or_paragraph`.

**Plugin impact:** `logos`'s `VerseChunker` must satisfy the same invariant. This is
SDK-breaking — see P1-6.

### P1-4 — Passage offsets

```sql
-- 004_passage_offsets.py
ALTER TABLE core.passages ADD COLUMN char_start int, ADD COLUMN char_end int;
CREATE INDEX passages_doc_span_idx ON core.passages (document_id, char_start, char_end);
```

Nullable at first so existing rows survive the migration. After P1-5 has re-chunked
everything, a follow-up migration sets `NOT NULL`.

### P1-5 — Non-destructive re-chunk

**The hazard.** `extractions` and `mentions` are `ON DELETE CASCADE` on `passages.id`
(`schema.py:120-127`, `:243-249`); `events.source_passage_id` and
`edges.source_passage_id` are `SET NULL`. Deleting passages to re-chunk therefore
destroys every extraction and mention in the corpus and silently nulls event and edge
provenance. Today, re-chunking is a data-loss operation wearing the costume of a
migration.

**The complication nobody expects.** Re-anchoring by offset overlap is the obvious
algorithm and it is unavailable: old `prose_window` passages have `byte_start: 0`, so
there are no valid old offsets to overlap against. Re-anchoring must go through
**text matching**.

This is tractable because the damage is known and bounded: `" ".join(...)` only
collapses whitespace runs. Normalize whitespace on both sides and the old passage text
becomes an exact substring of the normalized canonical text.

**Algorithm**, per document, in one transaction:

1. Load or reconstruct canonical text into `document_texts`.
2. Run the new chunker; insert new passages under the new `chunker_version`. The
   unique constraint permits coexistence with the old rows — this is why step 3 can
   safely precede step 4.
3. For each old passage: whitespace-normalize its text, locate it in the
   whitespace-normalized canonical text, map back to a raw span, find the new
   passage(s) covering that span, and repoint `extractions.passage_id`,
   `mentions.passage_id`, `events.source_passage_id`, `edges.source_passage_id` to the
   new passage with the greatest overlap.
4. Delete the old passages. Cascade now has nothing left to destroy.

**Unmatched old passages** (OCR variance, ambiguous overlap) must not be silently
dropped. Write them to a `reindex_orphans` report with document, old passage id, and
the dependent-row counts, and **fail the run** if orphans exceed a threshold
(suggest 0.5%). A silent 3% loss of extractions is exactly the failure this phase
exists to prevent.

**Ship as:** `research-engine reindex chunks [--document-id …] [--dry-run]`.
`--dry-run` reports the orphan count without writing — run it across the whole corpus
before the real pass.

**Tests** (`integration`): a document with extractions, mentions, an event, and an
edge survives re-chunk with every reference repointed and none nulled; a deliberately
unmatchable passage appears in the orphan report; `--dry-run` writes nothing.

### P1-6 — SDK version bump

P1-3 breaks the chunker protocol; P3 adds a bibliographic client; §3.3 adds a locator
contribution type. Good news: `check_core_api` is enforced at `loader.py:105`, so the
mechanism to refuse stale packs already works. (The CHANGELOG's "no runtime
enforcement yet" is stale — fix that line while you are here.)

**Batch every SDK-breaking change into one release.** Bump `core_api` to `0.3.0`
once, publish a migration note per pack, and update all five in the fleet. Five packs
is bounded coordination now; it is unbounded once third-party packs exist.

---

## P2 — Vector index and evaluation (2–3 weeks)

Sequenced immediately after P1 so both the chunker change and the index change can be
measured. Shipping an ANN index without eval means shipping an unmeasured regression
in filtered recall, and filter extensions are a headline feature.

### P2-1 — Typed, indexed embeddings

Per S2, default to **one typed table per dimension**:

```sql
-- 005_typed_embeddings.py
CREATE TABLE core.passage_embeddings_1024 (
  passage_id uuid NOT NULL REFERENCES core.passages(id) ON DELETE CASCADE,
  model text NOT NULL,
  model_version text NOT NULL,
  embedding vector(1024) NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (passage_id, model, model_version)
);
CREATE INDEX passage_embeddings_1024_hnsw
  ON core.passage_embeddings_1024 USING hnsw (embedding vector_cosine_ops);
```

Copy existing rows across, then drop the old untyped table. `PGPassageRepo` dispatches
on `EmbeddingPort.dim`. A new dimension is a new table plus a migration — boring and
certainly correct, which is what infrastructure should be.

**HNSW over IVFFlat:** IVFFlat requires training on a representative sample and
degrades as the corpus drifts from it. A personal corpus accretes a few documents at a
time; HNSW builds incrementally and needs no retraining.

**Filtered recall.** Apply whatever S2 established. If `hnsw.iterative_scan` does not
recover recall under narrow filters, implement adaptive-k: when the post-filter yield
falls below the requested `k`, re-query with a widened `k` (say ×4, twice) before
giving up. Log when widening triggers — a filter that always widens is a candidate for
its own table or partial index.

### P2-2 — Evaluation harness

`08-search-and-extraction.md:211` and `11-implementation-architecture.md:1504` already
specify recall@10, MRR, and nDCG@10. Nothing implements them.

```
packages/core/src/research_engine/eval/
  __init__.py
  queryset.py     # QuerySet: [(query, relevant_passage_ids, note)]
  metrics.py      # recall@k, MRR, nDCG@k
  runner.py       # takes a container factory, not a container
```

**The harness must accept a container *factory*, not a container.** Fusion mode,
alpha, and rerank arrive via `SearchQuery`, but embedding model, reranker, and chunker
are constructor-injected in `composition.py`. A/B-ing those requires standing up
alternate wirings.

```
research-engine eval run --set golden.yaml --config a.toml --config b.toml
```

prints per-config metrics and a paired diff.

**Building the golden set.** Hand-labelling is the backbone; bootstrap it cheaply:
generate candidate queries per passage with an LLM, confirm in bulk once, freeze as
the regression set. Then let P4's annotations and saved queries feed new judgments as
a byproduct of real research — which is the argument for P4 preceding any hardening of
eval.

**Acceptance:** P1's chunker change and P2-1's index change each have a measured
before/after on the same query set. If either regresses recall, it does not ship.

---

## P3 — Bibliographic records and citation (3–4 weeks)

### P3-1 — Schema

Per design §3.1, storage is relational; CSL-JSON is a wire format only.

```sql
-- 006_bibliographic.py
CREATE TABLE core.bibliographic_records (
  document_id  uuid PRIMARY KEY REFERENCES core.documents(id) ON DELETE CASCADE,
  citekey      text UNIQUE,
  csl_type     text NOT NULL,
  title            text,
  container_title  text,
  volume text, issue text, pages text, edition text,
  publisher text, publisher_place text,
  doi text, isbn text, issn text, url text,
  accessed_at      timestamptz,
  issued_start     timestamptz,
  issued_end       timestamptz,
  issued_precision text,
  csl_extra    jsonb NOT NULL DEFAULT '{}',
  provenance   text NOT NULL
);
CREATE UNIQUE INDEX bib_doi_uq  ON core.bibliographic_records (lower(doi))  WHERE doi  IS NOT NULL;
CREATE UNIQUE INDEX bib_isbn_uq ON core.bibliographic_records (lower(isbn)) WHERE isbn IS NOT NULL;

CREATE TABLE core.bib_contributors (
  record_id uuid NOT NULL REFERENCES core.bibliographic_records(document_id) ON DELETE CASCADE,
  ordinal   int  NOT NULL,
  role      text NOT NULL,
  family text, given text, suffix text, particle text,
  literal   text,
  entity_id uuid REFERENCES core.entities(id),
  PRIMARY KEY (record_id, ordinal, role),
  CHECK (literal IS NOT NULL OR family IS NOT NULL)
);
CREATE INDEX bib_contributors_entity_idx ON core.bib_contributors (entity_id);
```

`issued_start`/`_end`/`_precision` deliberately mirror the
`created_date_start`/`_end`/`_precision` idiom already on `documents`, so partial
dates ("1863, circa") and ranges ("1861–1865") work. `timestamptz` is arguably wrong
for a publication year; matching the existing convention beats being locally right.

The partial unique indexes on DOI and ISBN are what make P3-4's work-clustering cheap
and what stop the same paper being recorded twice.

### P3-2 — Population

Three sources, priority-ordered, with the winner recorded in `provenance`:

1. **Plugins at ingest** — richest data (`acad` holds OpenAlex records, `books` holds
   Open Library). Add a `bibliographic` client to the SDK bundle in
   `plugins/sdk/clients.py`, gated by the existing `ingest` permission, with a
   `DeniedBibliographicClient` counterpart in `plugins/permissions.py` matching the
   established pattern.
2. **Core resolver** — `DOI → Crossref`, `ISBN → Open Library`. Also the repair path
   for everything ingested before this existed.
3. **LLM extraction from front matter** — last resort for scans with no identifier.
   Ship it as an extraction schema so it carries confidence and evidence like
   everything else, rather than as a bespoke code path.

### P3-3 — CSL mapping layer

```
packages/core/src/research_engine/services/citation/
  csl_export.py   # rows -> CSL-JSON   (~150 lines)
  csl_import.py   # CSL-JSON -> rows   (the expensive half — defer)
  inline.py       # rows -> "Smith 2019, 143"  (~30 lines)
```

**Stage the directions.** Export is cheap: `csl_type` is stored, so type mapping is
free, leaving a field-rename map (`pages`→`page`, `container_title`→`container-title`),
name assembly from `bib_contributors`, and date assembly from `issued_*` into
`date-parts`. Import is where the cost lives — Zotero emits `literal` names, raw date
strings, EDTF, and fields we do not model. **Ship export first**; it already delivers
Pandoc and Zotero-import. Defer `csl_import.py` until there is a real library to seed
from.

### P3-4 — Work identity

`search_sources` already computes DOI → ISBN → title+author+year matching at discovery
time and then discards the conclusion. Persist it: register `same_work` in core
`relation_types` and write an edge with confidence when a match is found. The
`EdgeClient` and `write` permission already exist.

Clustering is a recursive CTE over `edges`; materialize it only if latency demands.
Splitting a bad cluster is deleting one edge — which is the property a `work_id`
column would not have given us.

### P3-5 — Quote verification

The highest value-per-line tool here, and mostly unblocked by P1-2 rather than
difficult in itself.

`verify_quote(text, document_id?)` → `exact | normalized | near | not_found`, with
`passage_id`, locator, a Tier-1 inline reference, and for near-misses a diff against
the closest candidate.

**Normalization is where correctness lives**, not the SQL: NFKC, collapse whitespace,
unify curly/straight quotes and en/em dashes, strip soft hyphens, rejoin
line-break hyphenation. Each of these is a real OCR artefact in a scanned corpus.

**Offset mapping.** Normalization changes lengths, so a match in `normalized_text`
must map back to a raw offset. Build the `normalized_index → raw_index` map in a single
pass **on demand for the one matched document** rather than storing it — it is large,
cheap to recompute for a single document, and storing it would need invalidating
whenever `normalization_version` changes.

**Chunk-straddling** is why this searches `document_texts`, not passage text: a quote
spanning two passages matches neither. Search the document, then map the raw span onto
the passages covering it via `passages_doc_span_idx`.

**Report the tier honestly.** A researcher needs to know whether the source says
exactly this, or says it modulo OCR noise. Never collapse `normalized` into `exact`.

### P3-6 — Citation rendering (design §3.4, Option 4)

- **Tier 1 — inline references**, built directly from `bibliographic_records` +
  `bib_contributors`. `Smith 2019, 143`. In-process, ~30 lines, no dependency. This is
  what P3-5 returns on every call and what the agent uses mid-conversation.
  **Label it in the tool description as orientation during research, not publication
  output.** Unstated, someone pastes it into a manuscript.
- **Tier 2 — CSL-JSON at the boundary.** `export_bibliography(document_ids[], format)`
  → CSL-JSON, `.bib`, `.ris`. Style correctness belongs to the user's Pandoc or Zotero.

Do not bundle `citeproc-py`. When a user asks Tier 1 for a real journal style, that is
the signal to reconsider — not before.

**Retire the P0-1 interim.** With `bib_contributors.entity_id` populated,
`author_entity_id` becomes a real join and the name-match fallback is deleted. Remove
the warning log with it.

---

## P4 — Annotations and projects (2–3 weeks)

Specified to schema and decisions; the tool surface follows the established MCP
patterns and needs no special guidance.

### P4-1 — Annotations

```sql
CREATE TABLE core.annotations (
  id          uuid PRIMARY KEY,
  document_id uuid NOT NULL REFERENCES core.documents(id) ON DELETE CASCADE,
  char_start  int NOT NULL,
  char_end    int NOT NULL,
  passage_id  uuid REFERENCES core.passages(id) ON DELETE SET NULL,  -- cache only
  body        text,
  tags        text[] NOT NULL DEFAULT '{}',
  color       text,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX annotations_doc_span_idx ON core.annotations (document_id, char_start, char_end);
CREATE INDEX annotations_tags_idx     ON core.annotations USING gin (tags);
```

**`passage_id` is a refreshable cache and must be `SET NULL`, never the anchor.** The
anchor is `(document_id, char_start, char_end)` — that is what survives a re-chunk. Any
future table tempted to anchor on `passage_id` should be reviewed against P1-5.

Do not model annotations as `extractions` with `owner='user'`: extractions are
immutable, LLM-provenanced, and keyed by `(passage_id, schema_id, extractor_version)`;
annotations are mutable, human, and unversioned. Forcing them together corrupts both.

Add `has_annotation` and `tagged_with` filter extensions — registered in
`composition.py` alongside `EventDateRangeFilter` and `HasExtractionFilter`, and
remembering P0-1: a new filter key needs a repository branch or it now raises.

Ship one-way markdown **export**, not Obsidian sync. Export captures most of the
interop value without the bidirectional consistency problem.

### P4-2 — Projects

The primitive that four separate gap-list items turn out to be views of: screening and
PRISMA, saved queries, project-scoped search, reproducibility manifests, and eval query
sets. A `projects` table (question, created, state), membership edges to
documents/passages with per-candidate state (`screened_in`/`screened_out` + reason), a
decision log, and saved queries.

PRISMA-specific reporting belongs in a `systematic-review` **plugin** on top, not in
core.

---

## P5 — Outline only

Deliberately not specified in detail; each item depends on spike outcomes or on what
P0–P4 teach us.

- **Synthesis (§7)** — gated on S4. Needs a claim-embedding layer first:
  `extraction_records` are currently queryable only by `record_type` and JSONB, with no
  semantic search, so claim clustering has nothing to cluster on. The durable output is
  `supports`/`contradicts`/`refines` edges with confidence and both source passages —
  the LLM adjudicates, the graph is what persists.
- **Figures and tables (§8.1)** — tables become passages with a `content_kind`
  discriminator; figures get their own `assets` table with caption-derived embeddings.
  The blocking sub-problem is that chunkers must not split a table, which is a *second*
  chunker-contract change and should be designed alongside P1 if it is wanted soon.
- **Re-embedding (§8.2)** — unblocked by P2-1. Write into a new typed table, index it
  while the old serves traffic, require 100% coverage, then flip `active_embedding_model`.
- **Archival (§9.1)** — SavePageNow by default, local snapshot for material that must
  not be publicly archived. Encode the borrowed-library prohibition as a
  permission-level rule, not a README note: `kindle` and `ycl` ingest licensed content
  and the failure mode is republishing it.
- **Manifests (§9.2)** — P4-2's project object serialized: identity, not content.

---

## Appendix A — Migration sequence

| # | Name | Phase | Reversible |
|---|---|---|---|
| 003 | `document_texts` | P1-2 | yes |
| 004 | `passage_offsets` | P1-4 | yes |
| 005 | `typed_embeddings` | P2-1 | yes (keep old table one release) |
| 006 | `bibliographic` | P3-1 | yes |
| 007 | `passage_offsets_not_null` | after P1-5 completes | yes |
| 008 | `annotations` | P4-1 | yes |
| 009 | `projects` | P4-2 | yes |

Every one is additive. The only destructive step in the plan is P1-5's deletion of old
passages, which happens inside the re-anchoring transaction and after a `--dry-run`.

## Appendix B — Test strategy

- **unit** — pure logic: chunker invariant, metrics, CSL export mapping, normalization.
- **integration** — anything touching SQL: filter branches, language union, re-anchor,
  HNSW recall.
- **contract** — the chunker invariant applied to plugin-supplied chunkers, so
  `logos`'s `VerseChunker` is held to it too.

Three tests are load-bearing and should be written before the code they guard:

1. **Chunker invariant** (P1-3) — `draft.text == text[char_start:char_end]` for every
   chunker. Absent this, the class of bug this whole phase exists to fix recurs.
2. **`SearchFilters` reflection** (P0-1) — every field has a repository branch. Absent
   this, silent filters return.
3. **Re-anchor survival** (P1-5) — extractions, mentions, events, and edges all survive
   a re-chunk. Absent this, the migration is a data-loss event.

## Appendix C — Rollback

P0 is code-only; revert the commit.

P1 is the risk. Old and new passages coexist under different `chunker_version` values
until P1-5's final delete, so rollback before that step is a matter of deleting the new
rows. **After the delete, rollback requires the pre-migration backup** — take one with
`research-engine backup create` immediately before the production re-chunk run, and
verify it restores before proceeding.

P2–P4 are additive: revert the migration, revert the code.
