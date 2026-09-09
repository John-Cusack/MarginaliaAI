# Design: Closing the research-workflow gaps

**Status:** Draft / RFC
**Scope:** `packages/core`, the SDK contract, and the plugin fleet
**Audience:** implementers; assumes familiarity with `03-architecture.md` and `07-pack-system.md`

---

## 0. Framing

Corpus Engine is strong across **discover → acquire → ingest → retrieve → extract**.
The gaps cluster at the two ends of the research loop that the engine doesn't
model: **reading/annotating** and **writing/citing**. A prior audit enumerated
eleven gaps. This document does the design work: for each, two or three viable
designs, the trade-offs, a recommendation, and the seams it touches.

Three things shape every recommendation below.

**Design principle 1 — the graph is the durable artifact; the LLM is a transient
adjudicator.** Anything an LLM concludes should land in `edges` / `extractions`
with confidence and a source passage, so it is inspectable, correctable, and
citable. Tools that reason at call time and return prose are not reproducible,
and reproducibility is the product.

**Design principle 2 — prefer interchange formats over internal models.** The
vision says "we integrate rather than replace" citation managers. Choosing the
formats the ecosystem already speaks (CSL-JSON, PRISMA, WARC/memento) converts
N×M mapping problems into one adapter each.

**Design principle 3 — don't anchor to things that move.** See §1. This is the
constraint that sequences the entire plan.

### The dependency that reorders everything

Five of the eleven gaps — pin-cites, quote verification, annotations, figures/tables,
and re-chunking — all need the same missing primitive: **a stable, document-relative
address for a span of text.** It does not exist today, and the default chunker
actively destroys the information needed to build it.

That makes §1 a prerequisite, not a nice-to-have. Everything else sequences behind it.

---

## 1. Cross-cutting: the anchoring problem

### 1.1 What's actually broken

`ProseWindowChunker._make_draft` (`chunking/prose_window.py:66-76`) emits:

```python
locator={"byte_start": 0, "byte_end": len(text.encode())}
```

`byte_start` is **always 0**. `byte_end` is the length of the *chunk*, not an
offset into the document. Every passage produced by the default chunker claims to
begin at byte zero of its document. The locator carries no positional information
whatsoever.

`prose_window` is the `default_chunker` for `books`, `kindle_book`, and `ycl_book`
document types — i.e. most of the corpus. `FixedWindowChunker` does compute real
offsets (`fixed_window.py:33-45`), but `.strip()`s the chunk text afterwards, so
the stored offsets drift from the stored text by the amount of stripped whitespace.

Worse for reconstruction: `prose_window` splits on `_SENT_BOUNDARY` (`\s+` between
sentences) and rejoins with `" ".join(...)`. Paragraph structure and newlines are
destroyed, and windows overlap by `overlap_tokens`. **Document text cannot be
reconstructed from its passages** — naive concatenation both loses whitespace and
duplicates overlap regions.

### 1.2 The second-order problem: cascade deletes

`extractions`, `mentions`, `events.source_passage_id`, and `edges.source_passage_id`
all reference `passages.id`. The first two are `ON DELETE CASCADE`
(`schema.py:120-127`, `schema.py:243-249`); the latter two are `SET NULL`.

Re-chunking means deleting and reinserting passages. Today that operation
**destroys every extraction and mention in the corpus and silently nulls the
provenance of every event and edge.** Re-chunking is therefore not "unimplemented" —
it is currently a data-loss operation wearing the costume of a migration. Any
design that adds more passage-anchored data (annotations especially) makes this
worse.

### 1.3 Options

**Option A — canonical document text + document-relative offsets.**
Store the parser's output verbatim (`documents.canonical_text`, or a
`document_texts` side table). Chunkers become pure functions `text → [(start, end)]`
and passages store real `char_start`/`char_end` into that text. All annotation,
extraction, and figure anchoring targets `(document_id, char_start, char_end)`.
`passage_id` remains as a denormalized convenience column, refreshed on re-chunk.

- *Pros:* one addressing scheme for everything downstream; re-chunking becomes safe
  (recompute the `passage_id` cache, nothing else moves); quote verification and
  pin-cites fall out; figures and tables get somewhere to point.
- *Cons:* storage duplication (text stored once canonically and again across
  overlapping passages); a breaking chunker-contract change; requires re-ingest of
  the existing corpus to populate.
- *Storage note:* a 500-page book is ~1 MB of text. 10k documents ≈ 10 GB before
  TOAST compression. Acceptable for a single-user Postgres.

**Option B — content-addressed passage identity.**
Derive `passage.id` from `hash(normalized_text)` so re-chunking preserves IDs for
unchanged boundaries.

- *Pros:* no new storage; no chunker contract change.
- *Cons:* only helps when boundaries *don't* move, which is precisely the case where
  you weren't going to lose anything anyway. Changing `max_tokens` re-cuts every
  boundary and orphans everything. Solves the easy half of the problem.

**Option C — re-anchoring migration only.**
Keep passage anchoring; when re-chunking, map old→new passages by text overlap and
rewrite foreign keys in the same transaction.

- *Pros:* smallest change; no new columns; no re-ingest.
- *Cons:* the mapping is lossy and ambiguous (one old passage overlaps two new ones —
  which extraction survives?). Needs bespoke logic per referencing table. Solves
  re-chunking but delivers nothing for pin-cites, quotes, annotations, or figures.

**Option D — treat each chunker as producing a separate document.**
Never delete; new chunker version = new passage set alongside the old.

- *Pros:* no data loss by construction; the `(document_id, position, chunker,
  chunker_version)` unique constraint already permits it.
- *Cons:* storage multiplies per chunker version; search must be told which chunker
  to query or it returns near-duplicates; doesn't give offsets either.

### 1.4 Recommendation

**Option A, with Option C as the one-time bridge for the existing corpus.**

Option A is the only one that pays for five downstream features rather than one.
The chunker contract change is the real cost and it should be taken deliberately
and early, before annotations add another anchored table.

Concretely:

1. Add `document_texts(document_id PK, text, normalized_text, encoding_version)`.
   Keep it out of `documents` so the hot path doesn't drag a megabyte per row.
2. Change the chunker protocol from `chunk(text) -> [PassageDraft]` to returning
   drafts carrying mandatory `char_start`/`char_end` into that exact text. Promote
   `locator` to a typed model (§3.3) with the offsets as a required base.
3. Add `passages.char_start` / `char_end` as real indexed columns, not JSON.
4. Fix `prose_window` to track offsets through the sentence split rather than
   rejoining with `" "`. This changes its output text (whitespace preserved), so it
   is a `chunker_version` bump to `2.0`, which the unique constraint already models.
5. Ship `research-engine reindex chunks` that re-chunks under the new version and
   re-anchors extractions/mentions by offset overlap (Option C logic), run once.

**Seams touched:** `domain/passages.py`, `ports/repositories.py` (`PassageRepo`),
all four core chunkers, `logos`'s `VerseChunker` (SDK-breaking — see §9),
`services/ingestion/orchestrator.py`, a new migration.

**Risk to spike first:** confirm that every ingestion module's output is stable
enough to be the canonical text. `DoclingModule` calls `export_to_markdown()`
(`docling_converter.py:170`) whose output can shift between docling versions.
If canonical text is version-dependent, offsets are too — which argues for storing
the text rather than recomputing it, and for treating `parser_version` as part of
the anchoring contract.

---

## 2. Correctness fixes (prerequisite, ~1 week)

These are bugs, not features. They should land before anything else because two of
them silently corrupt research results.

### 2.1 Silently ignored filters

`find_passages` advertises `author_entity_id` and `recipient_entity_id`;
`SearchFilters` also carries `language` (`domain/passages.py:82-88`).
`filter_candidate_ids` (`repositories/passages.py:235-295`) reads none of the three.
`hybrid.py:47` then sets `applied_filters = filter_dict`, so the response
**affirmatively reports a filter that never ran.**

Two decisions, and the second matters more than the first:

- *Implement them.* `author_entity_id` needs a join path. There isn't one: nothing
  populates author entities at ingest. The honest implementation joins
  `mentions → entities` with a role, which requires §3 (bibliographic authorship)
  to mean anything. Interim: filter on `documents.metadata->>'author'` and document
  the limitation.
- *Fail loudly on unknown filters.* Change `filter_candidate_ids` to raise on filter
  keys it doesn't handle, and derive `applied_filters` from what the query builder
  actually consumed rather than from the input dict. A silently-dropped filter in a
  research tool is worse than an error, because it produces a plausible wrong answer
  that no one audits. This pattern will recur every time a filter is added; fix the
  pattern, not the instance.

### 2.2 Hardcoded English text search

`"english"` is literal at `hybrid.py:73,90` and `orchestrator.py:173,245`, though
`passage_fts.lang_config` is a proper `regconfig` column with a default
(`001_initial.py:76-81`) and `documents.language` exists.

The embedding model is `BAAI/bge-m3` (`local_bge.py:12`) — genuinely multilingual.
So vector recall is fine for German, French, Latin, Greek, while keyword recall is
degraded by English stemming, and RRF fuses a good ranked list with a bad one.
The `logos` pack ships biblical-studies material; this is live.

Design question: **what language does a query get searched in?**

- *Per-document at index time* is unambiguous: use `documents.language`, fall back
  to a detector, fall back to `simple` (no stemming) rather than `english` — `simple`
  degrades gracefully for unknown languages where `english` degrades wrongly.
- *Per-query at search time* is the hard half. A German query against a mixed corpus
  should not be stemmed as English. Options: (a) detect the query language and search
  only matching-language rows; (b) fan out one `to_tsquery` per distinct
  `lang_config` present in the candidate set and union; (c) add a `language` search
  filter and let the caller decide, defaulting to fan-out.

  Recommend **(b) with (c) as an override.** Fan-out is a small number of configs in
  practice, keeps recall high for mixed-language corpora, and avoids betting on a
  language detector for three-word queries.

Also fix `index_fts` (`repositories/passages.py:203-214`): the `ON CONFLICT DO
UPDATE` clause updates `ts` but not `lang_config`, so re-indexing a passage under a
new language leaves the column disagreeing with the vector it describes.

### 2.3 No vector index — and why it can't be added as-is

`001_initial.py:64-73` creates `embedding vector NOT NULL` — **unconstrained
dimension**. pgvector cannot build an ivfflat or HNSW index on a column without a
fixed dimension. So this is not a forgotten `CREATE INDEX`; the schema as written
forbids one. Every vector search is a sequential scan with exact distance
computation over the whole table, at `k_vec=100` per query.

The tension: the `(passage_id, model, model_version)` primary key deliberately
allows several embedding models to coexist — which is the mechanism §8 needs for
zero-downtime re-embedding. Different models have different dimensions (bge-m3 =
1024, bge-small = 384, text-embedding-3-large = 3072). A single fixed-dim column
would forbid that.

**Option A — pin the column to `vector(1024)`, index it, accept one model.**
Simplest. But it deletes the multi-model capability and makes §8's re-embedding
migration require downtime. A trap: cheap now, expensive exactly when you need it.

**Option B — partition `passage_embeddings` by `(model, model_version)`**, each
partition typed to its own dimension with its own HNSW index.
**This does not work and is recorded here so it is not re-proposed.** Postgres
partitions inherit the parent's column types exactly; a partition cannot narrow
`vector` to `vector(1024)` while a sibling uses `vector(3072)`. Partitioning by model
buys operational conveniences (cheap `DROP` of one model's vectors) but not
heterogeneous dimensions, which is the property that mattered.

**Option C — partial expression indexes** (`... USING hnsw ((embedding::vector(1024))
...) WHERE model = 'BAAI/bge-m3'`). Lighter than a table split, but the query must
match the index expression exactly for the planner to use it, which is fragile under
refactoring and needs verifying that HNSW accepts a cast expression at all.

**Option D — one typed table per dimension** (`passage_embeddings_1024`,
`passage_embeddings_3072`), each `vector(N) NOT NULL` with its own HNSW index; the
repository dispatches on the active model's dimension. A new dimension is a new table
plus a migration.

**Recommendation: Option D**, with Option C measured against it in spike S2 before
being adopted. D is boring and certainly correct, and it preserves the multi-model
coexistence the PK already promises — which is what keeps §8.2's re-embedding a
background operation rather than a downtime event. Option A remains a trap for that
reason.

Choose **HNSW over IVFFlat**: IVFFlat requires training on a representative sample
and degrades as the corpus drifts past the trained distribution, which is wrong for a
corpus that accretes a few documents at a time. HNSW builds incrementally.

**The subtlety that will bite:** ANN indexes and the filter-extension system
interact badly. `vector_search` currently applies `passage_id = ANY(:candidate_ids)`
as a `WHERE` clause. With a sequential scan that is a true pre-filter and correct.
With an HNSW index it becomes a **post-filter**: the index returns the 100 globally
nearest, then the filter discards most of them, and a narrow filter can return two
rows where it should return a hundred. Filter extensions are a headline feature and
they will quietly stop working well the day the index lands.

Mitigations, in preference order: pgvector 0.8+ `hnsw.iterative_scan = relaxed_order`;
adaptive `k` widening when the post-filter yield is low; and for filters that are
both common and low-cardinality, promote them to partition keys. Whichever is
chosen, **§6's eval harness is what proves it works** — which is an argument for
sequencing eval earlier rather than later.

---

## 3. Bibliographic identity, citation, and quotation

The largest user-visible gap. Grep across all six repos returns zero hits for
bibtex, CSL, RIS, or Zotero.

### 3.1 Where does bibliographic metadata live?

Today: `documents` has `title`, `document_type`, `language`, `source`, dates, and an
untyped JSON `metadata`. Authors, journal, volume, issue, pages, publisher, DOI,
ISBN, edition all live in that JSON, with each of the five plugins inventing its own
shape.

**Option A — typed columns on `documents`.**
Add `authors[]`, `container_title`, `volume`, `issue`, `pages`, `doi`, `isbn`,
`issued`, `edition`.
- *Pros:* directly queryable and indexable; one obvious place to look.
- *Cons:* bakes one bibliographic model into the core schema, and the model that
  fits a journal article fits an archival manuscript badly; every new material type
  is a migration; conflates "what the parser found" with "what we believe the
  citation is."

**Option B — store CSL-JSON as a JSONB blob**, one row per document, with a few
generated columns (`doi`, `isbn`, `issued_year`, `csl_type`) promoted for indexing.
- *Pros:* perfect round-trip fidelity with Zotero and Pandoc; zero mapping code; new
  CSL fields need no migration.
- *Cons:* defeats constraints and typed queries; schema drift is invisible;
  **contributors are not joinable**, which leaves §2.1's `author_entity_id` filter
  unfixable without fuzzy string matching. Rejected — see the recommendation.

**Option C — keep it in `documents.metadata`, but define and validate a schema.**
- *Pros:* cheapest; no migration.
- *Cons:* no indexing without generated columns anyway; no separation of parser
  output from curated truth; correcting a citation means mutating the parser's record.

**Option D — normalized `bibliographic_records` + `bib_contributors`, with a JSONB
tail for the long tail of CSL fields.** Columns for the ~18 fields that are queried,
constrained, or indexed; a real contributors table; `csl_extra jsonb` for archival
fields (`archive`, `call-number`, box/folder) that would otherwise be a migration per
material type.

**Recommendation: Option D.**

Two decisions were conflated in an earlier draft of this document and must be kept
separate:

- **Storage format → relational.** JSONB as the primary store defeats exactly the
  properties (constraints, indexes, typed queries, reviewable migrations) that make
  Postgres worth using.
- **Interchange format at the import/export boundary → CSL-JSON.** This is
  independent of storage. CSL-JSON is *rendered* from relational rows on the way out
  and parsed into them on the way in. Design principle 2 still applies; it applies to
  the wire, not to the disk.

This is also how the ecosystem does it — Zotero stores relationally and exports
CSL-JSON; Crossref is relational. Nobody stores the blob.

**The argument that decides it for this codebase:** §2.1's broken `author_entity_id`
filter needs a join path from documents to `entities` through authorship. A
`bib_contributors` table with a nullable `entity_id` FK *is* that path. Store authors
in JSON and the filter either stays broken or gets a fuzzy string-match
implementation that silently returns wrong results — reintroducing the precise
failure mode §2.1 exists to eliminate.

Three properties of bibliographic data force structure and should be designed
against deliberately:

1. **Names are not strings.** `van der Berg`, `Smith, Jr.`, corporate authors,
   non-Western name order. CSL splits `family` / `given` / `particle` / `suffix` /
   `literal` because every simplification breaks alphabetization somewhere. Ordered
   (author order is semantic), roled, and joinable — a table.
2. **Dates are not `date`.** "1863, circa" and "1861–1865" don't fit. Reuse the
   `created_date_start` / `_end` / `_precision` idiom already established on
   `documents`. (`timestamptz` is arguably the wrong type for a publication year, but
   matching the existing convention beats being locally correct.)
3. **Fields are sparse and type-dependent.** `volume` is meaningless for a webpage;
   `container-title` means journal, book, or website depending on `csl_type`. Modelling
   all ~70 CSL fields yields a mostly-null table, so the tail goes to `csl_extra`.

Sketch:

```sql
CREATE TABLE core.bibliographic_records (
  document_id  uuid PRIMARY KEY REFERENCES core.documents(id) ON DELETE CASCADE,
  citekey      text UNIQUE,              -- CSL "id"; what appears in [@smith2019]
  csl_type     text NOT NULL,            -- article-journal | book | chapter | manuscript
  title            text,
  container_title  text,
  volume text, issue text, pages text, edition text,
  publisher text, publisher_place text,
  doi text, isbn text, issn text, url text,
  accessed_at      timestamptz,          -- see §9.1
  issued_start     timestamptz,
  issued_end       timestamptz,
  issued_precision text,
  csl_extra    jsonb NOT NULL DEFAULT '{}',
  provenance   text NOT NULL             -- plugin | resolver | llm | manual
);

CREATE TABLE core.bib_contributors (
  record_id uuid NOT NULL REFERENCES core.bibliographic_records(document_id) ON DELETE CASCADE,
  ordinal   int  NOT NULL,
  role      text NOT NULL,               -- author | editor | translator
  family text, given text, suffix text, particle text,
  literal   text,                        -- corporate authors
  entity_id uuid REFERENCES core.entities(id),
  PRIMARY KEY (record_id, ordinal, role),
  CHECK (literal IS NOT NULL OR family IS NOT NULL)
);
```

*Cost:* a CSL-JSON mapping layer, roughly 300 lines — a field correspondence table
plus special-casing for names and dates. That is the price of the constraints, and
it is worth paying.

**Who populates it?** Three sources, in priority order, with the winner recorded in
a `provenance` field on the record:
1. **Plugins at ingest** — they hold the richest data (`acad` has OpenAlex records,
   `books` has Open Library). Add a `bibliographic` method to the SDK client bundle.
2. **A core resolver** — `DOI → Crossref`, `ISBN → Open Library`. Also a repair tool
   for documents ingested before this existed.
3. **LLM extraction from front matter** — last resort for scans with no identifier.
   An extraction schema, so it carries confidence and evidence like everything else.

### 3.2 Work vs. edition identity

Preprint and published version, two editions, or the same book from Open Library +
Internet Archive + Kindle + YCL currently produce unrelated documents. Dedup is
`(content_hash, source)` (`orchestrator.py:195-199`), which by construction cannot
see this.

The irony worth exploiting: **`search_sources` already performs exactly this match**
at discovery time (DOI → ISBN → title+author+year) and then discards the conclusion
at ingest.

**Option A — `work_id` column + a `works` table.** Fast clustering queries.
But work identity is a judgment call; a wrong assignment is corrected by rewriting
rows, and there's nowhere to record confidence or who decided.

**Option B — `same_work` / `manifestation_of` edges** in the existing `edges` table.
Zero schema change: edges already carry `source_kind`, `target_kind`,
`relation_type`, `confidence`, `attributes`, and `source_passage_id`. Clustering is
a recursive CTE. Splitting a bad cluster is deleting one edge.

**Option C — an explicit cluster table with membership rows.** Between the two;
buys little that B doesn't.

**Recommendation: Option B**, promoted to a materialized view if clustering
latency becomes real. It reuses infrastructure, it is correctable, it records
confidence — which matches how fuzzy the match genuinely is — and the work is mostly
*persisting a decision `search_sources` already makes*. Register `same_work` in core
`relation_types`; the `write` permission and `EdgeClient` already exist to let
plugins assert it.

### 3.3 Pin-cites and the locator model

`passages.locator` is untyped JSON, and per §1.1 the default chunker fills it with
zeros. Even fixed, the shape varies legitimately: page numbers, verse references
(logos), Kindle locations, TEI `<ref>` targets, timecodes.

**Option A — a typed discriminated union in core** (`PageLocator`, `SectionLocator`,
`CharOffsetLocator`, `TimecodeLocator`) with a `kind` discriminator.
**Option B — a locator contribution type**, registered by plugins exactly as
`filter_extensions` are, each supplying a schema and a `format()` renderer.
**Option C — leave untyped; add a `format_locator()` convention.**

**Recommendation: A for the core set, B as the escape hatch.** Core owns
`char_offset` (mandatory, from §1) plus page/section/timecode; plugins register
additional variants through a `locators` contribution in `pack.yaml`. This mirrors
the filter-extension pattern already proven in the codebase, so it fits the grain
rather than inventing a second extension idiom. `char_offset` being mandatory and
universal is what makes quote verification and annotations possible regardless of
which exotic locator a plugin adds on top.

### 3.4 Citation rendering and export

Once §3.1 exists, the question is what actually renders a citation. CSL-JSON buys
exactly one thing — **not owning a citation formatter**. It is the calling convention
for citeproc, Pandoc, and Zotero. If none of those are called, it earns nothing: you
would be serializing rows to JSON in order to parse them back into a string you could
have built directly.

**Option 1 — direct formatters from rows, no CSL anywhere.**
- *Pros:* no dependency; one data path; in-process and fast; golden-string testable;
  no untrusted-input parsing; full control over pin-cites, which CSL handles awkwardly
  (locators are a citeproc concern, not a CSL-JSON data concern).
- *Cons:* you own citation correctness permanently; doesn't scale past ~2 styles; dead
  end for Pandoc/Zotero. **The signature is the tell:** `format(record) -> str` is
  pure, but real formatting isn't — ibid, first-vs-subsequent, and `2019a`/`2019b`
  disambiguation all need document context, so adding them later is a redesign rather
  than a feature.

**Option 2 — CSL-JSON export only; the engine never formats.**
- *Pros:* correctness fully outsourced; ~150 lines; one output reaches Pandoc, Zotero,
  citeproc-js/py, and ~10k styles; the format has been stable for a decade.
- *Cons:* **the agent cannot answer "cite this"** — for a tool whose primary interface
  is an LLM in conversation, returning a JSON blob to a citation request is a bad
  answer. Imposes a toolchain requirement. Serialization bugs surface inside Pandoc,
  far from your tests. Serves §3.5 not at all.

**Option 3 — CSL-JSON plus bundled `citeproc-py`.**
- *Pros:* full style coverage in-process; best agent UX; stateful features handled
  correctly; shares the serializer with Option 2.
- *Cons:* **dependency risk is the sharp one** — `citeproc-py` is thinly maintained
  and its CSL coverage lags citeproc-js, so this bets citation correctness on the
  weaker of the two implementations purely because we are in Python. Bundling CSL
  style files is a distribution, licensing (CC-BY-SA), and update problem. Too slow
  for §3.5's hot path, so a fast path gets added anyway — arriving at Option 4 with
  extra steps. Unsupported CSL features yield subtly wrong output rather than errors.

**Option 4 — two tiers: inline references from rows, CSL-JSON for anything
style-specific.**
- *Pros:* each path used where it is strong — fast and deterministic for the constant
  case, ecosystem leverage for the hard case. Ownership is bounded to one trivial
  convention (~30 lines) rather than a style system. Ibid, disambiguation, and journal
  quirks are never ours. Both paths read the same tables, so there is one source of
  truth. Incrementally adoptable, and leaves Option 3 available as a pure addition —
  the serializer's output *is* citeproc's input.
- *Cons:* two surfaces that can disagree (inline says `Smith 2019`, Pandoc's
  bibliography says `Smith, J. (2019a)`); two things to test; and the inline
  convention is a house style matching no journal.

**Recommendation: Option 4**, with the inline tier explicitly scoped as
**orientation during research, not publication output**. Unstated, that scoping is a
footgun — someone will paste an inline ref into a manuscript. When a user asks the
inline tier to support a real journal style, that is the signal to collapse into
Option 3, and not before.

Concretely:

- **Tier 1 — inline references, built directly from `bibliographic_records` +
  `bib_contributors`.** `Smith 2019, 143`. In-process, ~30 lines, no dependency.
  This is what §3.5 returns on every quote verification and what the agent uses when
  citing a passage mid-conversation.
- **Tier 2 — CSL-JSON at the boundary.** `export_bibliography(document_ids[], format)`
  → CSL-JSON (and `.bib` / `.ris` via the same mapping). Handed to the user's Pandoc
  or Zotero, which owns style correctness.
- `import_bibliography(file)` → seeds `bibliographic_records`, optionally creating stub
  documents for material not yet held.

**Stage the two directions.** Export (rows → CSL-JSON) is ~150 lines: `csl_type` is
already stored so type mapping is free, leaving a field-rename map, name assembly from
`bib_contributors`, and date assembly from `issued_*`. Import is the expensive half —
Zotero emits `literal` names, raw date strings, EDTF, and fields we do not model.
Export-only delivers Pandoc and Zotero-import at half the cost; defer import until
there is an actual library to seed from.

**Decision rule if the workflow differs:** Pandoc or LaTeX → Option 4. Word or Google
Docs with one fixed style → Option 1 is genuinely defensible; skip the CSL layer
entirely. Never start at Option 3.

**On stakes:** all four options read the same tables, so this is a two-way door.
1→4 adds a serializer; 2→4 adds a formatter; 4→3 slots citeproc behind the existing
serializer. The irreversible decision was relational-versus-blob storage in §3.1, and
that is what makes the rendering choice cheap. Take the smallest option that serves
the current writing workflow.

### 3.5 Quote verification

The highest value-per-line tool in this document, and mostly blocked on §1 rather
than on its own complexity. Extraction schemas already enforce
`evidence_must_be_substring: true` (`claims.yaml`), so the discipline exists; it
just isn't exposed to the human writing the paper.

`verify_quote(text, document_id?)` → exact match | normalized match | not found,
with `passage_id`, locator, a Tier-1 inline reference (§3.4), and for near-misses a
diff against the closest candidate.

Two real design problems:

**Chunk-straddling.** A quote spanning two passages matches neither. This is why the
tool needs canonical document text (§1) rather than passage text — search the
document, then map the offset back to the passage(s) it falls in.

**Normalization is where the correctness actually lives.** Curly vs. straight quotes,
ligatures from OCR, soft hyphens, en/em dashes, non-breaking spaces, and
line-break hyphenation in scanned text. Store a `normalized_text` alongside the
canonical text with a recorded `normalization_version`, search that, and map offsets
back through the transform. Report the match tier honestly — a researcher needs to
know whether the source says exactly this or merely says it modulo OCR noise.

---

## 4. Annotations

Zero hits for annotation, highlight, note, or tag anywhere in core. The corpus is
**write-only to the LLM and read-only to the human**: extractions, edges, and events
are machine-authored, while the researcher's own objections and marginalia have
nowhere to live. For a project named Marginalia, the conspicuous absence.

**Option A — a core `annotations` table.** `(id, document_id, char_start, char_end,
passage_id (denormalized), body, tags[], color, created_at, updated_at)`, shaped
like `mentions`.
- *Pros:* first-class query target — "passages I tagged `methodology` that also
  mention X" — which is the whole point. Composes with the filter-extension system
  (`has_annotation`, `tagged_with`). Feeds §6's eval sets for free.

**Option B — reuse `extractions` with `owner='user'`.**
- *Cons:* wrong model in every dimension. Extraction records are immutable,
  LLM-provenanced, and keyed by `(passage_id, schema_id, extractor_version)`.
  Annotations are mutable, human, and unversioned. Forcing them together corrupts
  both. Reject.

**Option C — external files (Obsidian/markdown) synced in.**
- *Pros:* matches "integrate rather than replace"; researchers already have note
  systems.
- *Cons:* annotations aren't queryable in the corpus and can't drive filters, which
  is most of the value. Bidirectional sync is a known-hard consistency problem.

**Recommendation: Option A, plus one-way *export* to markdown.** Export captures
most of the interop value of C at a fraction of the cost and with no consistency
problem. Revisit sync only if export proves insufficient in practice.

**Anchor to `(document_id, char_start, char_end)`, never to `passage_id` alone** —
per §1, that is the difference between annotations surviving a re-chunk and
evaporating during one. Keep `passage_id` as a refreshed cache column.

---

## 5. The research project as a first-class object

The audit listed "no systematic-review workflow" as its own gap. It is better read
as a symptom: **there is no object representing a piece of research in progress.**
Everything is global-corpus-scoped and stateless, while an academic runs three
projects at once.

**Option A — build screening specifically:** `screening_sessions`,
`screening_decisions`, PRISMA counts.
**Option B — build it as a plugin** over existing edges and annotations.
**Option C — introduce a core `projects` primitive:** a named research question with
a candidate set, per-candidate state (`screened_in` / `screened_out` + reason),
saved queries, and a decision log.

**Recommendation: Option C.** Four separate items on the gap list are views of this
one object:

- **Screening / PRISMA** — candidate states plus the decision log, with a
  `systematic-review` *plugin* adding PRISMA-specific reporting on top.
- **Saved queries** — a query belongs to a project; re-running it is reproducibility.
- **Project-scoped search** — `find_passages(project_id=...)` as a filter extension,
  which is the thing that makes a 50k-document corpus usable for a specific paper.
- **Reproducibility manifests (§9)** — a manifest is a project serialized.
- **Eval query sets (§6)** — a project's saved queries plus its annotated passages
  *are* a relevance-judgment set, collected as a byproduct of real work.

Building screening alone gets one of these. Building the project object gets all
five, and each subsequent one costs a tool rather than a subsystem.

---

## 6. Retrieval evaluation

`08-search-and-extraction.md:211` and `11-implementation-architecture.md:1504`
specify recall@10, MRR, and nDCG@10. Nothing implements them and no golden set
exists. Consequently there is no way to know whether reranking helps, whether
`alpha=0.5` is defensible, or whether a chunker change improved retrieval — and
§1 proposes changing the chunker, while §2.3 proposes changing the index in a way
that is *known* to threaten filtered recall.

**That inverts the naive ordering.** Eval is not a late-phase quality item; it is
the instrument that tells you whether §1 and §2.3 worked. It should land early
enough to measure them.

**Option A — hand-built golden set + offline scorer**, pytest-driven over a fixture
corpus. Deterministic and CI-able; costs manual labeling.
**Option B — LLM-as-judge**, generating queries from passages and grading relevance.
Cheap to bootstrap, noisy, and drifts as the judge model changes.
**Option C — implicit signal**: which results the researcher annotated or cited.
Zero labeling cost, sparse, accumulates slowly.

**Recommendation: A as the backbone, bootstrapped by B, continuously enriched by C.**
Generate candidate queries with B, confirm them in bulk by hand once, freeze as the
regression set. Then let §4's annotations and §5's saved queries feed new judgments
as a byproduct of real research — the reason §4 and §5 should precede eval hardening.

One design constraint: the harness must A/B *configurations* (fusion mode, alpha,
rerank on/off, chunker, embedding model, ANN parameters). `HybridSearchService`
takes those partly via `SearchQuery` but partly via constructor injection
(`embedding`, `reranker`). The harness should therefore accept a **container
factory**, not a container, so it can stand up alternate wirings.

Minimum viable: `research-engine eval run --set golden.yaml --config a.toml
--config b.toml` printing per-config recall@k / MRR / nDCG@k and a paired diff.

---

## 7. Synthesis primitives

The vision promises contradiction detection, evolving positions, and gap-finding.
Grep: nothing. The substrate exists and is unused — `claims.yaml` already extracts
`stance` as a −1..1 float with an evidence span, events carry timestamps, and edges
are typed with confidence. Meanwhile edges are only ever written as `cites`, by one
plugin.

**Option A — LLM-orchestrated tools.** `find_contradictions(claim)` retrieves and
adjudicates at call time. Fast to build; expensive per call; non-reproducible —
two runs give two answers, which disqualifies it for citation.
**Option B — a materialized claim layer.** Extract claims corpus-wide, embed the
claim texts, cluster by topic, compute stance disagreement within clusters, persist
`contradicts` edges. Reproducible and cheap to query; expensive batch job; quality is
bounded by extraction quality.
**Option C — hybrid.** Materialize and embed claims; tools retrieve over the claim
layer and use the LLM only to adjudicate a shortlist; persist the verdict as an edge.

**Recommendation: Option C**, per Design principle 1. The durable output is a
`contradicts` / `supports` / `refines` edge with confidence and both source
passages — inspectable, correctable, and citable, which is the entire point for an
academic.

This surfaces a genuine prerequisite: **`extraction_records` are not semantically
searchable.** `query_records` filters by `record_type` and JSONB
(`ports/repositories.py`), with no embedding. Synthesis over claims needs a claim
embedding layer analogous to `passage_embeddings`. That is the real work item here;
the tools on top are thin. Also register `supports` / `contradicts` / `refines` in
core `relation_types`.

---

## 8. Figures, tables, and corpus lifecycle

### 8.1 Figures and tables

`docling_converter.py:85-88` sets `do_picture_description=False` and
`generate_picture_images=False` — figures are dropped outright. Tables run through
TableFormer but are then flattened by `export_to_markdown()` (`:170`) into the text
stream and shredded by a prose chunker.

**Recommendation: split the two, because they are different data.**

- **Tables → passages** with a `content_kind` discriminator (`prose` | `table` |
  `figure` | `equation`) and the structured cells in `metadata`. A table serialized
  as markdown retrieves acceptably through the existing hybrid stack, and its caption
  makes it findable. Minimal new schema.
- **Figures → an `assets` table** (`document_id`, `kind`, `char_start`, `caption`,
  `file_path`, `embedding`). Figures are binary; their retrievable surface is the
  caption and surrounding prose, so they want a caption-derived embedding and a path,
  not a passage row.

**The blocking sub-problem:** the chunker must not split a table. This needs a
structure-aware pre-pass marking atomic spans that chunkers may not cut. That is
real work, it changes the chunker contract a second time, and it should therefore be
designed *together with* §1 rather than bolted on afterwards.

### 8.2 Re-embedding

Gated on §2.3's partitioning. With it: `research-engine reindex embeddings --model X`
writes rows under a new `(model, model_version)` into a fresh partition, indexes it
while the old partition serves live traffic, and a readiness check requires 100%
coverage before an `active_embedding_model` setting flips reads over. Without
partitioning this requires downtime — the concrete reason §2.3 Option A is a trap.

### 8.3 Re-chunking

Gated on §1. Safe once passages are offset-anchored and the re-anchoring migration
exists; a data-loss operation until then.

### 8.4 Cost

`llm_calls.cost_estimate` is faithfully recorded and never read back. Two cheap wins
whose seam already exists — both LLM adapters already receive `llm_calls_repo` in
`composition.py:150-163`:

- An `llm_usage` MCP tool / CLI report (by purpose, caller, model, window).
- A `BudgetGuard` wrapper implementing the LLM port, refusing calls past a
  configured ceiling. Corpus-wide extraction runs are exactly the operation that
  surprises people with a bill.

---

## 9. Archival, portability, and the SDK contract

### 9.1 Link rot

Plugins fetch live pages; nothing snapshots them and there is no `accessed_at`.

**Recommendation: archive.org SavePageNow by default, local snapshot as an option.**
A memento URL is more durable than a local WARC, and — decisively — it is directly
citable in a paper, which a local file is not. Store `accessed_at` and the memento
URL on the bibliographic record.

**Constraint worth encoding, not just documenting:** never push borrowed-library or
paywalled content to a public archive. The `kindle` and `ycl` packs ingest exactly
that. This should be a permission-level or document-type-level prohibition rather
than a note in a README, because the failure mode is republishing licensed material.

### 9.2 Reproducibility manifests

`backup create/restore` is a `pg_dump` wrapper — fine for disaster recovery, useless
for "here is the corpus this paper was written from."

Export a **manifest of identity, not content**: document list with CSL-JSON records
and content hashes, passage counts, embedding model + version, chunker + version,
plugin set + versions, and the project's saved queries (§5). Rights problems mostly
evaporate because the text isn't in it, and a reviewer with equivalent access can
reconstruct and verify. This is §5's project object serialized.

### 9.3 The SDK contract needs to hold still — or announce when it moves

Good news first: the CHANGELOG's claim that `core_api` has "no runtime enforcement
yet" is **stale**. `check_core_api` is called at `loader.py:105` and incompatible
packs are refused. The mechanism works.

The problem is what this plan does to the contract. §1 changes the chunker protocol
(breaking `logos`'s `VerseChunker`), §3.1 adds a bibliographic client, §3.3 adds a
locator contribution type, and §7 adds relation types. That is a `0.3.0` at minimum
and arguably `1.0.0`.

**Recommendation:** batch the SDK-breaking changes into one release rather than
dribbling them across five. Publish a migration note per plugin, and bump
`core_api` once so the loader's existing enforcement refuses stale packs loudly
instead of failing at runtime in the middle of an ingest. Five plugins are in the
fleet; the coordination cost is real but bounded, and it is much cheaper now than
after third-party packs exist.

---

## 10. Sequencing

Ordered by dependency, not by value. Estimates assume one engineer.

| Phase | Work | Depends on | Est. |
|---|---|---|---|
| **P0** | §2.1 fail-loud filters, §2.2 language config + `index_fts` fix | — | 1 wk |
| **P1** | §1 canonical text + offsets + chunker contract; §3.3 locator model; §8.1 atomic-span pre-pass | P0 | 3–4 wk |
| **P2** | §2.3 embedding partitioning + HNSW; §6 eval harness v1 | P1 (to measure it) | 2–3 wk |
| **P3** | §3.1 bibliographic tables + CSL mapping layer; §3.2 `same_work` edges; §3.4 export; §3.5 quote verification | P1 | 3–4 wk |
| **P4** | §4 annotations; §5 project object | P1 | 2–3 wk |
| **P5** | §7 claim embeddings + synthesis; §8.2–8.4 lifecycle; §9 archival + manifests | P2, P3, P4 | 4–6 wk |

Two notes on the ordering.

**P1 is unglamorous and blocks five features.** The temptation will be to ship
quote verification or citation export first because they are what a researcher
actually feels. Resist it: built on today's locators they would be built on
`byte_start: 0`.

**P2 pulls eval earlier than instinct suggests.** It is placed immediately after the
chunker change and alongside the index change specifically so both can be measured.
Shipping the HNSW index without eval means shipping an unmeasured regression in
filtered recall, and filter extensions are a headline feature.

---

## 11. What to spike before committing

Four assumptions carry most of the risk. Each is a few days.

1. **Canonical-text stability.** Is `DoclingModule.export_to_markdown()` output
   stable across docling versions? If not, offsets are parser-version-dependent —
   which is survivable (store the text, treat `parser_version` as part of the
   anchoring contract) but must be known before §1 is designed around it.
2. **ANN filtered-recall.** Build an HNSW index on a realistic corpus and measure
   recall under narrow filter extensions, with and without `iterative_scan`. If
   relaxed-order iterative scan doesn't recover it, §2.3 needs partition-by-filter
   and the design changes materially.
3. **CSL-JSON coverage for the actual corpus.** Map ten real documents spanning
   `acad`, `books`, `logos`, and `kindle` onto CSL-JSON by hand. If archival and
   biblical material strains it badly, §3.1 needs a documented extension convention
   before implementation rather than after.
4. **Claim-extraction quality.** Run `claims.yaml` over a few hundred passages and
   inspect. §7 is only worth building if extracted stance is good enough to cluster
   on; if it isn't, that phase is a research problem wearing an engineering costume,
   and it should be scoped down to `supports`/`contradicts` edges asserted by hand.

---

## Appendix: findings index

Every claim above, with its location.

| # | Finding | Location |
|---|---|---|
| 1 | `prose_window` locator is always `byte_start: 0` | `chunking/prose_window.py:66-76` |
| 2 | `fixed_window` strips text after computing offsets | `chunking/fixed_window.py:33-45` |
| 3 | Extractions/mentions cascade-delete on passage delete | `schema.py:120-127`, `:243-249` |
| 4 | `author_entity_id`/`recipient_entity_id`/`language` filters ignored | `repositories/passages.py:235-295` |
| 5 | `applied_filters` reports unapplied filters | `services/search/hybrid.py:47` |
| 6 | `"english"` hardcoded | `hybrid.py:73,90`; `orchestrator.py:173,245` |
| 7 | `index_fts` never updates `lang_config` on conflict | `repositories/passages.py:203-214` |
| 8 | `embedding vector` has no dimension → no index possible | `001_initial.py:64-73` |
| 9 | No ANN index of any kind | `001_initial.py` (absence) |
| 10 | N+1 passage loads per search | `hybrid.py:145-160` |
| 11 | Figures disabled in docling | `docling_converter.py:85-88` |
| 12 | Tables flattened to markdown text | `docling_converter.py:170` |
| 13 | `cost_estimate` recorded, never read | `schema.py` (`llm_calls`); no reader |
| 14 | No bibtex/CSL/RIS/Zotero anywhere | all six repos (absence) |
| 15 | No annotation/note/tag table | core (absence) |
| 16 | Dedup is `(content_hash, source)` only | `orchestrator.py:195-199` |
| 17 | Edges only ever written as `cites` | `acad` pack only |
| 18 | `core_api` enforcement *does* exist (CHANGELOG stale) | `loader.py:105` |
