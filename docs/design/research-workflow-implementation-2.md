# Implementation Guide II: Remediation, Retrieval, and Citation

**Companion to:** `research-workflow-gaps.md` (design/RFC),
`research-workflow-implementation.md` (guide I — P0/P1, **delivered**)
**Status:** Phase R ready now; P2 ready pending S2; P3 ready pending S3

---

## How to use this document

Guide I covered P0 (correctness) and P1 (the anchoring foundation). Both shipped.
This one covers what is left, reordered by what running the code on a real corpus
actually taught us.

**Scope.** Phase R and P2 are specified to implementable detail. P3 is specified
to schema and decisions plus the parts P1 unblocked. P4 is schema-level. P5 stays
an outline. Same discipline as guide I: detail where it is earned, outline where
it would be fiction.

**What changed since guide I was written.** Three things, all from measurement
rather than reasoning, and all of them move the plan:

| Assumption in guide I | What the corpus actually shows |
|---|---|
| Heterogeneous embedding dimensions are a design risk to plan around | They are already here — dim 1024 and dim 8 in one column — but the dim-8 rows are **test pollution**, so P2-1 gets to assume a single dimension |
| `hnsw.iterative_scan` availability is unverified, so S2 might force adaptive-k | pgvector is **0.8.2**; `iterative_scan` exists and is directly testable |
| Filtered ANN recall is the main P2 risk | It still is, but the bigger finding is there is **no vector index at all** — 1,490 ms per search |

---

## Verified state

Measured against the live dev database, not assumed.

| Fact | Value |
|---|---|
| Core version | `0.3.0` |
| Migrations at head | `004_passage_offsets` — next revision is `005` |
| Tests | 587 unit (1 skipped) + 28 integration, green |
| Documents | 2,728 |
| Passages | 271,172 |
| Documents **with** canonical text | 5 (the P1 verification ingests) |
| Documents **without** canonical text | 2,723 |
| Chunker versions in corpus | `prose_window` 2.0, `verse_boundary` 2.0 — all current |
| `passage_embeddings` | 1.47 GB, only a btree PK, **no vector index** |
| Embedding models present | `BAAI/bge-m3` dim 1024 (269,077) and `fake-test-embedder` dim 8 (2,095) |
| pgvector / pg_trgm | 0.8.2 / 1.6 |
| Postgres | 15.17 |

---

## Phase R — Remediation (≈1 week)

Four defects found by running the code against real data rather than fixtures.
None is a missing feature; all three of the first ones make the system quietly
wrong today. They come first for the same reason P0 did.

### R-1 — Configuration does not load

**Problem.** `Settings` declares `env_file = ".env"`, but nothing is being read
from it: `anthropic_api_key` resolves unset and `db_url` resolves to the
hardcoded default even though the engine is plainly reaching a database.
`RE_DEFAULT_LANGUAGE=en` appended to `.env` had no effect. `.mcp.json` supplied
no `env` block at all, so the MCP server ran on defaults for every setting.

**Why it matters beyond language.** `llm_budget_usd` is configured the same way.
A budget ceiling that silently fails to load is worse than no ceiling, because it
reads as protection.

**The design.** Config resolution must be observable and must fail loudly when
asked for something it cannot honour.

- `research-engine config show` — print every setting, its resolved value, and
  **where it came from** (default / env var / env file). Secrets shown as
  `SET`/`unset`, never echoed.
- `load_settings()` logs at startup which env file it resolved and whether it
  existed. `env_file` is relative to the process working directory, which is the
  actual trap here: the CLI run from a different directory reads a different
  file, or none.
- Resolve `env_file` to an absolute path anchored at the package root, with an
  `RE_ENV_FILE` override, so behaviour stops depending on cwd.

**Acceptance:** `research-engine config show` names the file it read; changing a
value in that file changes the reported value; a setting that came from nowhere
says `default`.

### R-2 — Real books carry fake embeddings

**Problem.** Twelve `ycl_book` documents hold 2,361 passages, of which **266**
have a real `bge-m3` embedding and **2,095** have only `fake-test-embedder` at
dim 8. The YourCloudLibrary plugin's integration suite defaults to the dev
database (`DEFAULT_DB_URL` pointing at `localhost:5435`), ingests real books
through the real orchestrator, and fakes only the embedder. Nothing cleans up.

`vector_search` filters on `model`, so the fake rows never surface in results —
which is exactly why nobody noticed. The damage is the *absence*: 2,095 passages
of real library books are invisible to semantic search. Keyword search still
finds them, which makes the gap look like a ranking quirk rather than missing
data.

**Fix, in order:**

1. `research-engine embeddings backfill [--model …] [--dry-run]` — find passages
   with no embedding under the active model and embed them. Batched, resumable,
   reports counts. This is a tool worth having permanently: it is also how you
   recover from an interrupted ingest.
2. Delete the `fake-test-embedder` rows. They are inert for search but they are
   the only reason `passage_embeddings` holds two dimensions, and P2-1 is much
   simpler against a single dimension.
3. Add an integrity check to `corpus_stats`: passages with no embedding under the
   active model, and embeddings whose `dim` disagrees with the active model.
   Surfacing this in the tool the agent already calls means the next occurrence
   is noticed in a session rather than a year later.

**Acceptance:** every passage in the corpus has exactly one `bge-m3` embedding;
`passage_embeddings` holds one dimension; `corpus_stats` reports zero unembedded.

### R-3 — Plugin tests write to the production corpus

**Problem.** R-2 is a symptom. The cause is that a plugin integration suite
treats the researcher's live database as scratch space. Core's
`tests/integration/conftest.py` solved this during P1 — every row a test creates
is tracked and deleted, and nothing truncates `core.*` — but the pattern lives in
core and the plugins never got it.

**Fix.** Publish the isolation contract as part of the SDK rather than as advice:

- Ship `research_engine.testing` with the `Corpus` helper from core's conftest —
  `add_document`, `add_passage`, `track(table, id)`, `cleanup()` — so packs get
  correct isolation by importing it rather than by reimplementing it.
- Default plugin integration suites to a **separate database**
  (`research_engine_test`), created and migrated on demand. A pack's tests should
  not be able to reach the real corpus by default, whatever their conftest says.
- Add a `contract`-marked test asserting that a pack's integration suite leaves
  `core.documents` and `core.passage_embeddings` counts unchanged.

Five packs is bounded coordination now. It is unbounded once third-party packs
exist, which is the same argument that batched the SDK break in P1-6.

### R-4 — The corpus is unanchored

**Problem.** 2,723 of 2,728 documents have no canonical text, so nothing in the
corpus can be quote-verified or re-anchored. `reindex chunks` correctly refuses
them rather than guessing.

**This is not urgent and should not be rushed.** Nothing reads canonical text
yet. It becomes load-bearing when P3-5 ships, and re-ingesting before P3 exists
means paying the cost twice if P3 changes what gets stored.

**The design.** Backfill without re-embedding wherever possible. Every one of the
19 non-`logos` source paths was checked and **still resolves on disk**, which
splits the work into three tiers of very different cost:

- **16 plain-text sources** — 12 `ycl_book` and 3 `kindle_book` extractions under
  `~/.marginalia/plugins/*/extracted/*.txt`, plus one markdown file. No docling,
  no network, no auth: read the file, store it as canonical text, re-chunk. This
  tier is close to free and should be done first, as a rehearsal.
- **3 PDFs** (`generic`, 394–731 MB of scanned Civil War and Napoleonic material)
  — docling on CPU, hours each. Worth queueing overnight rather than blocking on.
- **2,704 `logos_book` documents** — canonical text can only come from re-walking
  the article chain, which P1 exercised end to end on one book (71 articles,
  305 passages, ~30 s). Extrapolating, the full 11 resources are dominated by the
  four large lexicons.

Two things the path check also surfaced, both worth fixing while in here:

- One `ycl` source (`a1mynhz9.txt`) backs **two** documents — a dedup miss, since
  `find_by_hash` keys on `(content_hash, source)` and the plugin path in
  `ingest_drafts` does not consult it at all.
- `ycl` and `kindle` ingest *licensed* content into `extracted/*.txt`. R-4 makes
  that text durable in `document_texts`, which is exactly the material §9.1 says
  must never be published. Encode it as a permission-level rule before the
  archival work, not after.
- **Plugin-ingested documents** (2,708 `logos_book`, 12 `ycl_book`) — canonical
  text can only come from the pack that fetched it. For `logos` this means
  re-walking, which the P1 work already exercised end to end on one book.
- **Order it after P3-1**, so a document is re-ingested once and comes back with
  its bibliographic record populated at the same time.

Ship `research-engine reindex text --dry-run` to report, per document, whether
canonical text is recoverable and by what route. That report is the input to
deciding how much of the corpus is worth re-ingesting at all.

**DONE — outcome.** `reindex text` classifies by cost, and the split is stark:

```
2723 document(s) without canonical text:
  fast              16  recoverable now (lightweight parser), 8 MB
  slow               3  recoverable, but needs docling, 1,651 MB
  unreachable     2704  not recoverable by core   (logos_book)
```

The 16 fast documents were recovered and re-anchored. **Every one of their 4,595
passages relocated in the recovered text — zero orphans.** That is the result
that matters: re-parsing a plain-text source reproduces exactly the text the
original passages were cut from, so the recovery route is sound rather than
merely plausible.

Two decisions this outcome supports:

- **Separating recovery from re-anchoring was right.** `reindex text` stores the
  substrate; `reindex chunks` re-anchors and reports orphans. The orphan count is
  the evidence that a recovered text is the *same* text — without the separation
  there would be nothing to check it against, only a hope.
- **The 3 docling PDFs stay behind `--include-slow`.** 1.65 GB of scanned Civil
  War and Napoleonic material is hours of CPU, and spike S1 has still not
  established whether docling output is reproducible across versions. Until it
  has, re-parsing them is a re-anchoring event of unknown cost, not a backfill.

**Follow-up this run exposed.** `reindex chunks` embeds inside each document's
transaction, so a book-length document holds a write transaction open for
minutes of GPU work. That was the right call when a half-embedded document was
unrecoverable — but R-2 shipped `embeddings backfill`, which changes the
calculus. Embedding can now move *outside* the transaction: the critical section
shrinks to insert-repoint-delete, and a crash leaves a document that is
keyword-searchable, detectable via `embeddings status`, and repaired by a tool
that already exists. Worth doing before the 2,704-document logos pass, where
transaction duration stops being a curiosity.

---

## Spikes

### S2 — pgvector index and filtered recall *(gates P2-1)* — **now cheap**

Guide I could not resolve two questions. Both are now directly testable.

**Resolved by inspection:** pgvector is **0.8.2**, so `hnsw.iterative_scan` is
available. The fallback plan of hand-rolled adaptive-k does not need designing
until `iterative_scan` is measured and found wanting.

**Simplified by R-2:** once the dim-8 rows are gone the corpus has exactly one
embedding dimension. Design A (one typed table per dimension) collapses to *one
typed table*, and the whole heterogeneous-dimension question can be deferred to
the day a second model is actually adopted — which is P5's re-embedding item.

**RUN — results below.** Measured on the real 271,172-vector corpus, every number
paired with the plan node that produced it. An earlier pass reported nonsense
(identical recall at `ef_search` 40 and 100; *higher* latency at 200 than an
exact scan) because it recorded whatever plan the planner chose rather than
verifying it — at `ef_search=200` the planner had silently reverted to a
sequential scan.

Build: 90 s, 2,044 MB, at `m=16, ef_construction=64`.

| ef_search | plan | median | recall@20 |
|---|---|---|---|
| — (exact baseline) | seq scan | 416 ms | 100% |
| 40 | hnsw | 2.09 ms | 60% |
| 100 | hnsw | 2.68 ms | 80% |
| 400 | hnsw | 3.46 ms | 80% |

**~150× faster.** Treat the recall column as a lower bound, not a measurement:
the probes were existing corpus vectors, and this corpus is four Greek and
Hebrew lexicons whose short articles are full of near-duplicates, so the "exact
top 20" is one arbitrary choice among many tied candidates and any equally-good
alternative scores as a miss. Real recall needs P2-2's judged query set — which
is the argument for building the harness before trusting any of this.

**The filtered-recall prediction was wrong, and that is the useful part.**

| filter | candidates | plan | rows | recall@20 | latency |
|---|---|---|---|---|---|
| 0.1% | 271 | bitmap/PK | 20/20 | 100% | 1.6 ms |
| 1% | 2,711 | seq scan | 20/20 | 100% | 12 ms |
| 10% | 27,117 | hnsw | 20/20 | 100% | 140 ms |

The post-filter catastrophe does not occur. Postgres pre-filters when the
candidate set is small and only reaches for HNSW once the set is large enough to
pay for itself — and recall stayed at 100% throughout. `hnsw.iterative_scan` made
no measurable difference at any selectivity, because nothing is post-filtering in
the first place.

**Decided:** ship `ef_search = 100` (`RE_HNSW_EF_SEARCH`). **Adaptive-k widening
is not needed** and should not be built. Revisit only if a filter shape appears
that the planner handles differently.

### S1 — Canonical-text stability *(gates R-4)*

Unchanged from guide I and now more valuable, because R-4's scale depends on it.
Parse the same 10 PDFs twice on the pinned docling version, then bump one minor
version and re-parse. If output is stable, canonical text is a cache and R-4 is
cheap to redo. If it drifts, `parser_version` is part of the anchoring contract
and re-parsing is a re-anchoring event. Assume drift.

### S3 — CSL coverage *(gates P3)*

Hand-map 10 documents spanning `acad`, `books`, `logos`, `ycl`, and `kindle` onto
the P3-1 column set. Record every field with nowhere to go. `logos` is the
interesting case: a "document" there is a *batch of articles*, not a work, so its
bibliographic identity belongs to the resource, not the document — see P3-1.

### S4 — Claim-extraction quality *(gates P5 §7)*

Unchanged. Run `claims.yaml` over ~300 real passages; inspect stance and topic by
hand. If stance is not reliable enough to cluster on, scope §7 down to
hand-asserted edges.

---

## P2 — Retrieval (2–3 weeks)

### P2-1 — Vector index

**The measurement that justifies this**, taken on the live corpus:

```
Parallel Seq Scan on passage_embeddings  (actual rows=89692 loops=3)
Execution Time: 1490.868 ms
```

Every semantic search reads 1.47 GB. Nothing else on the roadmap improves the
day-to-day experience this much.

**Do R-2 first.** With one dimension in the table this is a single migration:

```sql
-- 005_vector_index.py
ALTER TABLE core.passage_embeddings
  ALTER COLUMN embedding TYPE vector(1024);

CREATE INDEX CONCURRENTLY passage_embeddings_hnsw
  ON core.passage_embeddings USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);
```

Two notes that matter operationally:

- `CONCURRENTLY` cannot run inside a transaction, so the migration needs
  `autocommit_block()`. Without it Alembic wraps it and it fails.
- Typing the column requires every row to already be dim 1024. That is R-2's
  acceptance criterion, and the migration should assert it rather than trust it:
  fail with a clear message naming `embeddings backfill` if any row disagrees.

**HNSW over IVFFlat:** IVFFlat needs training on a representative sample and
degrades as the corpus drifts from it. A personal corpus accretes a few documents
at a time; HNSW builds incrementally and needs no retraining.

**Filtered recall** is where this can silently regress. Apply whatever S2
establishes; expose `ef_search` as a setting; log when a filtered search returns
fewer than the requested `k` so that a filter which always under-delivers is
visible rather than merely disappointing.

**When a second embedding model arrives**, revisit: one typed table per dimension
with the repository dispatching on `EmbeddingPort.dim`. Not before — and note
that this will break the YCL plugin's test conftest, which explicitly relies on
the column being unconstrained. That break is the point; R-3 fixes it properly.

### P2-2 — Evaluation harness

`08-search-and-extraction.md:211` and `11-implementation-architecture.md:1504`
already specify recall@10, MRR and nDCG@10. Nothing implements them.

```
packages/core/src/research_engine/eval/
  queryset.py     # QuerySet: [(query, relevant_passage_ids, note)]
  metrics.py      # recall@k, MRR, nDCG@k
  runner.py       # takes a container factory, not a container
```

**The harness must accept a container *factory*.** Fusion mode, alpha and rerank
arrive via `SearchQuery`, but embedding model, reranker and chunker are
constructor-injected in `composition.py`. A/B-ing those means standing up
alternate wirings.

```
research-engine eval run --set golden.yaml --config a.toml --config b.toml
```

**Do this before P2-1 ships, not after.** P1 already changed every passage
boundary in the corpus and P2-1 will change retrieval again; without a measured
baseline neither can be shown to have helped. Bootstrap the golden set by
generating candidate queries per passage with an LLM and confirming in bulk once,
then freeze it as the regression set.

**Acceptance:** P1's chunker change and P2-1's index change each have a
before/after on the same query set. If either regresses recall, it does not ship.

---

## P3 — Bibliographic records and citation (3–4 weeks)

This is the phase the whole exercise was for. P1 delivered its prerequisite: a
stable document-relative address for a span of text.

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

`issued_start`/`_end`/`_precision` mirror the `created_date_*` idiom already on
`documents`, so partial dates and ranges work. `bib_contributors.entity_id` is the
join that retires P0-1's author name-match hack.

**The `logos` problem, which S3 will surface and the schema must answer.** A
`logos_book` document is one *batch* of articles from a resource — the corpus
holds 2,708 of them for 11 actual books. Keying bibliographic records on
`document_id` would produce 2,708 records for 11 works, each claiming to be the
whole book.

Two options, decide in S3:

- **A — record on every batch document, cluster with `same_work` edges.** Uses the
  P3-4 machinery already planned, no new concepts, but the graph carries 2,700
  edges expressing one fact.
- **B — introduce a `works` table** that documents belong to, and hang the
  bibliographic record off the work. Correct FRBR modelling, and the natural home
  for "this PDF and that EPUB are the same book". Larger change.

**Recommendation: B**, because the batch-document shape is not a `logos` quirk —
any paginated or serialised source produces it, and `same_work` edges are the
wrong tool for a relationship that is structural rather than inferred.

### P3-2 — Population

Three sources, priority-ordered, winner recorded in `provenance`:

1. **Plugins at ingest** — richest data (`acad` holds OpenAlex, `books` holds Open
   Library). Add a `bibliographic` client to the SDK bundle, gated by the existing
   `ingest` permission, with a `DeniedBibliographicClient` matching the
   established pattern.
2. **Core resolver** — DOI → Crossref, ISBN → Open Library. Also the repair path
   for everything already ingested.
3. **LLM extraction from front matter** — last resort for scans with no
   identifier. Ship as an extraction schema so it carries confidence and evidence
   like everything else.

### P3-3 — CSL mapping

```
services/citation/
  csl_export.py   # rows -> CSL-JSON   (~150 lines)
  csl_import.py   # CSL-JSON -> rows   (the expensive half — defer)
  inline.py       # rows -> "Smith 2019, 143"  (~30 lines)
```

**Ship export first.** `csl_type` is stored, so type mapping is free, leaving a
field-rename map, name assembly from `bib_contributors`, and date assembly into
`date-parts`. That already delivers Pandoc and Zotero import. Defer `csl_import`
until there is a real library to seed from.

### P3-4 — Work identity

`search_sources` already computes DOI → ISBN → title+author+year matching at
discovery time and discards the conclusion. Persist it: register `same_work` in
core `relation_types` and write an edge with confidence. `EdgeClient` and the
`write` permission already exist. Clustering is a recursive CTE; materialize only
if latency demands. Splitting a bad cluster is deleting one edge.

If P3-1 option B is taken, `same_work` covers only *cross-work* identity
(this PDF ≡ that EPUB), and structural grouping goes through `works`.

### P3-5 — Quote verification

**Mostly built already.** P1 delivered `document_texts` with a trigram index,
`CanonicalIndex` for whitespace-tolerant lookup with offset mapping back to raw
text, the `normalize()` transforms, and `passages_doc_span_idx`. The end-to-end
path is verified working: a quotation with collapsed whitespace and curly quotes
resolved to characters 3326–3546 of the source and to the passage covering it,
with page numbers attached.

What remains is the tool surface:

`verify_quote(text, document_id?)` → `exact | normalized | near | not_found`,
with `passage_id`, locator, a Tier-1 inline reference, and for near-misses a diff
against the closest candidate.

- **`exact`** — found in raw canonical text.
- **`normalized`** — found after `normalize()`. Report this tier honestly and
  never collapse it into `exact`: a researcher needs to know whether the source
  says exactly this, or says it modulo OCR noise.
- **`near`** — trigram similarity above a threshold; return the diff.
- Corpus-wide search uses `document_texts_norm_trgm`; single-document search uses
  `CanonicalIndex` directly.

**Chunk-straddling is why this searches `document_texts`, not passage text**: a
quote spanning two passages matches neither. Search the document, then map the
raw span onto covering passages via `passages_doc_span_idx`.

**It only works on re-ingested documents.** Today that is 5 of 2,728. The tool
must say "this document has no canonical text" rather than "not found" — those
are different answers and conflating them would teach a researcher to distrust
the tool.

### P3-6 — Citation rendering (design §3.4, option 4)

- **Tier 1 — inline references** built directly from `bibliographic_records` +
  `bib_contributors`. `Smith 2019, 143`. In-process, ~30 lines, no dependency.
  What P3-5 returns on every call. **Label it in the tool description as
  orientation during research, not publication output**, or someone will paste it
  into a manuscript.
- **Tier 2 — CSL-JSON at the boundary.** `export_bibliography(document_ids[],
  format)` → CSL-JSON, `.bib`, `.ris`. Style correctness belongs to the user's
  Pandoc or Zotero.

Do not bundle `citeproc-py`. When a user asks Tier 1 for a real journal style,
that is the signal to reconsider — not before.

**Retire the P0-1 interim.** With `bib_contributors.entity_id` populated,
`author_entity_id` becomes a real join; delete the name-match fallback and its
warning log.

---

## P4 — Annotations and projects (2–3 weeks)

Schema and decisions only; the tool surface follows established MCP patterns.

### P4-1 — Annotations

```sql
-- 008_annotations.py
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

**`passage_id` is a refreshable cache and must be `SET NULL`, never the anchor.**
The anchor is `(document_id, char_start, char_end)` — that is what survives a
re-chunk. P1-5 already proves the pattern works; annotations should be added to
`DEPENDENTS` in `reindex.py` so the cache is repointed like everything else.

Do not model annotations as `extractions` with `owner='user'`: extractions are
immutable, LLM-provenanced and keyed by `(passage_id, schema_id,
extractor_version)`; annotations are mutable, human and unversioned.

Add `has_annotation` and `tagged_with` filter extensions, registered in
`composition.py` — and remember P0-1: a new filter key needs a repository branch
or `filter_candidate_ids` now raises.

Ship one-way markdown **export**, not Obsidian sync.

### P4-2 — Projects

The primitive that four gap-list items are views of: screening and PRISMA, saved
queries, project-scoped search, reproducibility manifests, and eval query sets. A
`projects` table (question, created, state), membership edges to
documents/passages with per-candidate state (`screened_in`/`screened_out` +
reason), a decision log, and saved queries.

PRISMA-specific reporting belongs in a `systematic-review` **plugin**, not core.

---

## P5 — Outline only

- **Synthesis (§7)** — gated on S4. Needs a claim-embedding layer first;
  `extraction_records` are queryable only by `record_type` and JSONB today, so
  claim clustering has nothing to cluster on. Durable output is
  `supports`/`contradicts`/`refines` edges with confidence and both source
  passages.
- **Figures and tables (§8.1)** — tables become passages with a `content_kind`
  discriminator; figures get an `assets` table with caption-derived embeddings.
  The blocking sub-problem is that chunkers must not split a table — a *second*
  chunker-contract change, and the invariant test from P1-3 is where it would be
  enforced.
- **Re-embedding (§8.2)** — where the typed-table-per-dimension design from guide
  I's §2.3 finally earns its keep. Write into a new typed table, index it while
  the old serves traffic, require 100% coverage, then flip the active model.
- **Archival (§9.1)** — SavePageNow by default, local snapshot for material that
  must not be publicly archived. Encode the borrowed-library prohibition as a
  permission-level rule, not a README note: `kindle` and `ycl` ingest licensed
  content and the failure mode is republishing it.
- **Manifests (§9.2)** — P4-2's project object serialized: identity, not content.

---

## Appendix A — Migration sequence

| # | Name | Phase | Notes |
|---|---|---|---|
| 005 | `vector_index` | P2-1 | needs `autocommit_block()` for `CONCURRENTLY`; asserts single dimension |
| 006 | `bibliographic` | P3-1 | plus `works` if option B |
| 007 | `passage_offsets_not_null` | after R-4 | only once every document is anchored |
| 008 | `annotations` | P4-1 | additive |
| 009 | `projects` | P4-2 | additive |

All additive. The only destructive step in the plan is R-2's deletion of
`fake-test-embedder` rows, which are garbage by construction.

## Appendix B — Test strategy

Guide I's three load-bearing tests are in place: the chunker invariant, the
`SearchFilters` reflection test, and re-anchor survival. Add:

1. **Embedding coverage** (R-2) — every passage has exactly one embedding under
   the active model, and every embedding matches the active dimension. Absent
   this, R-2 recurs the moment an ingest is interrupted.
2. **Plugin test isolation** (R-3) — a pack's integration suite leaves
   `core.documents` and `core.passage_embeddings` counts unchanged. Absent this,
   the next pack writes to the real corpus too.
3. **Filtered ANN recall** (P2-1) — recall@20 under a narrow filter stays above
   the S2-established floor. Absent this, the index ships an unmeasured
   regression in the headline feature.
4. **Verification tier honesty** (P3-5) — a quote that matches only after
   normalization reports `normalized`, never `exact`.

## Appendix C — Sequencing

```
R-1 ─┐
R-2 ─┼─> P2-1 (needs S2) ─> P2-2 baseline ─┐
R-3 ─┘                                     ├─> P3 (needs S3) ─> P4 ─> P5
R-4 ──────────────────────────────────────┘   (R-4 before P3-5 is useful)
```

R-1 through R-3 are independent and can run in parallel. R-4 is the long pole and
should follow P3-1 so each document is re-ingested once. P2-2 wants to exist
before P2-1 lands so the index change can be measured rather than assumed.
