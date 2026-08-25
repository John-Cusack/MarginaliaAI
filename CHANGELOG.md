# Changelog






### Docling structure comes from the document, not from its markdown

Structure was recovered by exporting markdown and running a heading regex back
over it. That is cheap, and it caps the structure layer at whatever survives the
export. Docling writes **every** heading as `##`, so a 2.9M-character book became
213 flat siblings, and page provenance — which Docling records for every single
item — was discarded entirely, leaving PDF locators at **0%**.

`DoclingModule` now walks the item stream and builds canonical text itself,
which is what `EPUBModule` already does with the spine. Offsets are exact by
construction rather than recovered. Measured against a real PDF, the built text
is 19,665 characters where `export_to_markdown()` gives 19,667 — a trailing
newline apart — and every section slices back out of it exactly.

- **Page provenance survives.** 31 page boundaries across pages 30–60 of
  *Campaigns of Napoleon*, where before there were none. Sections carry the page
  they start on, which is the slot `StructuralChunker` already reads
  (`chunking/structural.py`) and no module has ever filled, so passages get a
  page locator with no schema change.
- **`metadata["pages"]`** is a full offset→page boundary table, for spans that
  cross a page break. Same shape as the Logos pack's page markers.
- **Item labels survive**, so footnotes are distinguishable from body text.
- **Hierarchy does not improve, and cannot.** Docling detects a single heading
  level for these PDFs — `SectionHeaderItem.level` is 1 for all of them — so
  walking the model buys page numbers and labels, not nesting. Two other
  approaches were tried and rejected: `export_to_markdown(page_break_placeholder=…)`
  does not round-trip (stripping the placeholder gives 19,679 against 19,667,
  because it brings newlines with it), and pickling `DoclingDocument` across the
  worker boundary carries every bounding box for nothing the caller uses.

The parallel path now returns `(text, sections, pages)` per page range and the
parent shifts offsets onto the whole — page numbers are already absolute, so
only the offsets move.

Version 2.0: canonical text moves, so this is a re-ingest rather than a
re-chunk, and 1.0 documents are stale.

### Search returns what to read, not just what matched

A chunk is the right unit to embed and rank and the wrong unit to read: it ends
where the ingester happened to cut, which in a lexicon lands mid-definition. Every
hit now carries a `window` as well — prose read back from the document's canonical
text, bounded by the document's own structure and capped by a token budget.

Retrieval is untouched. Expansion happens after reranking, and a test asserts the
cross-encoder still receives chunk text, because letting a window reach it would
make scores depend on the read path and invalidate every stored baseline.

- **`PassageHit.text` still means "what matched"** — quote that. `window.text` is
  what to read. `window.source == "node"` means the window is a complete
  structural unit and `read_node` would add nothing.
- **Bounded by structure, then by budget.** Node spans here are wildly uneven —
  Louw-Nida's median is 68 characters, A Marginal Jew's p90 is 24,267, and the
  root node is the whole 23.2M-character document. So "read the containing node"
  fails at both ends and the rule needs a minimum as well as a maximum.
- **A window must be wider than the chunk to count as one.** Median
  passages-per-node is 1, so the deepest node is routinely the chunk itself; it
  clears any minimum while expanding nothing. Measured on the live corpus this
  returned `source="passage"` at 1.0x for a third of lexicon hits before it was a
  condition. On BDAG a 16-character fragment now expands to 3,077 characters.
- **The budget is script-aware**, sized from the hit's own text: the same token
  budget is a much shorter character window in Greek or Hebrew than in English.
  `approx_tokens` is measured on the returned text, not on the estimate that
  sized it.

**The read path lost its N+1 on the way.** It previously issued one `SELECT` per
id in two places — 50 single-row queries for a reranked `k=20` search, 20 of them
re-reading rows already read for the cross-encoder. Now three queries total,
constant in `k`: one for passages, one for ancestor chains, one for spans.

| | before | after |
|---|---|---|
| passage rows | 50 | 1 |
| ancestors | — | 1 |
| spans | — | 1 |

Also: `search_default_k`, `search_rerank_n` and `rrf_k` were declared and never
read — deleted rather than left as fiction alongside two settings that are real.
`PassageHit.context_available` was hardcoded `True` and never assigned; `window
is None` says the same thing honestly.

### Locators can be recovered without re-ingesting

Two repository additions that let a pack attach page numbers to material already
in the corpus:

- **`PassageRepo.set_locators`** — bulk-attach locators to existing passages. A
  locator is derived from the source rather than from the text, so learning it
  late invalidates nothing: not the chunk, not its offsets, not its embedding.
  Re-ingesting TDNT to add page numbers would re-embed 25,852 passages to change
  one JSON column on each.
- **`DocumentRepo.find_by_metadata`** — resolve documents by a key the pack
  wrote at ingest. The Logos pack had been storing a `core_document_id` on its
  staged chunks, which goes stale the moment a resource is re-ingested and then
  points at a document with no passages.

### `verify_quote` — check a quotation against what the source actually says

Search could find passages; nothing could take a quotation already written down
and confirm it, with a locator to cite. This closes that.

Five answers, and the distinctions are the point:

- **`exact`** — the source says precisely this.
- **`normalized`** — it says this apart from typography: curly quotes, dashes,
  line-break hyphenation, collapsed whitespace. **Never collapsed into
  `exact`.** Someone deciding whether to use quotation marks is relying on that
  difference, and merging the two would give a confident wrong answer instead of
  an honest hedged one.
- **`near`** — part of it matches; the response says where it diverges, and what
  the source has there instead.
- **`not_found`** — no document contains it.
- **`no_canonical_text`** — the named document has nothing stored to check
  against. Reporting that as `not_found` would teach a researcher to distrust a
  tool that was never given anything to read.

Three implementation notes worth knowing:

- **It searches document text, not passage text.** A quotation routinely
  straddles a chunk boundary, where it matches no passage at all. The resulting
  span is mapped back onto every passage it touches, and `straddles_passages`
  says when that happened.
- **`normalize_with_map`** folds typography while keeping a map back to raw
  offsets, so a `normalized` hit still reports the exact characters of the
  source. NFKC is applied per character rather than whole-string, because
  whole-string NFKC recombines a base character and a combining mark into one
  and a 2->1 contraction has no single raw offset — common in a corpus with
  Hebrew pointing and Greek accents. Safe only because the query goes through
  the same function.
- **Offset mapping runs on a window, not the document.** The largest document
  here is 23.2M characters; Postgres locates the match and the raw:normalized
  length ratio narrows it to a few KB before any Python touches it. Widening
  steps and a whole-document fallback cover the cases where that estimate is
  wrong.

Exposed as the `verify_quote` MCP tool and `research-engine verify-quote`.

### Reranking moved to the GPU host, and outages degrade instead of failing

`RE_EMBEDDING_BASE_URL` used to decide three unrelated things at once — where
compute runs, which model is authoritative, and whether a failure is fatal — so
a sleeping desktop took search down entirely, and the only way to disable
offload was to delete the address.

- **The inference server serves reranking too** (`--rerank-model`, on by
  default). Measured on this corpus: query embedding is 66 ms locally against
  ~20 ms remote, a wash, while reranking 30 candidates on a CPU-only host is
  48.8 s of a 49.1 s search. Reranking is the offload that pays for itself.
- **`RE_INFERENCE_BASE_URL`** is the new name, since one server now serves both
  models. `RE_EMBEDDING_BASE_URL` still works.
- **Three placement modes** for each model: `local_bge` (always here, and it
  *ignores* a configured host, so it is an off switch that does not make you
  delete the address), `remote_api` (always there, fail if unreachable), `auto`.
- **`auto` splits by workload.** A query embedding falls back to local — the
  vectors are interchangeable, measured bit-identical. A corpus-wide run does
  not, because silently moving 255k passages onto a laptop turns hours into days
  while nobody is watching.
- **An unreachable reranker skips reranking** rather than failing the search or
  spending 49 s on the CPU. `SearchResult.degraded` says so, the CLI prints it,
  and `find_passages` returns it.
- **A server too old to offer `/rerank` degrades under `auto`** and errors only
  under `remote_api`. The client is always upgraded before the server, so this
  version skew is the common case, not the exotic one — treating it as a fatal
  misconfiguration bricked every search against a still-perfectly-good host.
- **Circuit breakers were checked after the health handshake**, so a dead host
  was re-dialled on every call and the breaker never broke anything. Fixed in
  both remote clients.
- **`research-engine search` never ran.** A Typer group callback carrying a
  required argument makes Click demand a subcommand, so every invocation died
  with "Missing argument 'QUERY'". Registered as a plain command.
- **`search --json` was unparseable** — printed through rich, which wraps to
  terminal width and breaks lines inside JSON strings.

## 0.5.0 — 2026-08-11

### Added

- **Remote embedding offload.** `research-engine embed-server` serves a warm
  embedding model over HTTP from a machine with a fast GPU; set
  `RE_EMBEDDING_BASE_URL` and the engine embeds there instead of locally.
  Verified bit-identical to local output (`max|Δ| = 0`), so a corpus can be
  embedded from either machine interchangeably.

  Unlike the vidgen TTS offload this is modelled on, an unreachable server does
  **not** fall back to local. A vector is only comparable to vectors from the
  same model, so silently switching models mid-run would write points no index
  can relate — undetectable by any constraint. Instead the batch fails and
  `research-engine embeddings backfill` retries it later. Model identity is
  verified against `/health` before the first vector is stored, and enforced
  again server-side on every request.

  A circuit breaker opens after 3 consecutive failures so a dead server costs one
  timeout rather than one per batch.

### Fixed

- **`embedding_provider` was declared but never read.** `composition.py` always
  built `LocalBGEEmbedding`, so selecting `remote_api` did nothing — the same
  silently-ignored-configuration class as the `.env` resolution bug in 0.4.0.
- **Adaptive batch halving was in only one of the two places that embed.**
  `embeddings backfill` had it; `reindex chunks` did not, and two book-length
  documents failed with CUDA OOM during the R-4 re-anchor. The logic now lives in
  `services/ingestion/embed_batches.py` and both use it. A document whose
  passages cannot be embedded even individually now rolls back whole rather than
  committing half-searchable.

## 0.4.0 — 2026-08-10

Implements Phase R and P2 of `research-workflow-implementation-2.md`.

### Fixed

- **Configuration silently failed to load.** `env_file` resolved against the
  process working directory, so the file read depended on where the CLI was
  invoked from — and a missing env file is not an error, so an unset spend
  ceiling looked identical to a loaded one. Resolution now walks up from the
  working directory to the project root, honours `RE_ENV_FILE`, and is loud when
  that override points at nothing. `research-engine config show` reports every
  setting and *where the value came from*.
- **2,095 passages were invisible to semantic search.** Twelve real library books
  carried only an 8-dimensional stub vector written by the YourCloudLibrary
  plugin's integration suite, which ran against the live corpus. All are now
  embedded with the real model; the stub vectors are gone.
- **`ingest_drafts` identified documents by metadata, not content.** It hashed
  `source:title`, so re-ingesting a file under a differently-cased title produced
  a second document that the `(content_hash, source)` unique constraint could not
  catch — one library book is in the corpus twice as a result. It now hashes the
  content and returns the existing document instead of duplicating it.
- **`schema.py` declared four indexes the database did not have.** Three were GIN
  indexes on `json` columns, which Postgres rejects outright — they were fiction,
  and they made `metadata.create_all` fail. The fourth
  (`extractions_schema_idx`) was real and missing; migration `005` creates it.

### Added

- **HNSW vector index** (migration `006`). `passage_embeddings.embedding` is now
  `vector(1024)` with an HNSW index. Measured on 271k vectors: **416 ms → 2.7 ms**
  at `ef_search=100`. Filtered recall stayed at 100% across 0.1%/1%/10%
  selectivity — the predicted post-filter regression does not occur, so adaptive-k
  widening was not built. Tunable via `RE_HNSW_EF_SEARCH`.
- **`research_engine.testing`** — the `Corpus` isolation helper, `CorpusFootprint`
  for asserting a suite left no trace, and `resolve_test_db_url`, which steers
  pack test suites at `research_engine_test` unless
  `RE_TEST_ALLOW_REAL_CORPUS=1`. A pack's tests should not be able to reach the
  researcher's corpus by default.
- **`research-engine embeddings status | backfill | purge`** — coverage
  reporting and repair, with adaptive batch halving so a long passage that
  exhausts GPU memory splits instead of failing the run.
- **`research-engine eval run`** — recall@k, MRR, nDCG@k over a frozen query set,
  comparing configurations with a paired diff. The runner takes a container
  *factory*, since embedding model and reranker are constructor-injected.
- **`research-engine reindex text [--dry-run] [--include-slow]`** — recovers
  canonical text for documents ingested before `document_texts` existed, by
  re-parsing the source. Classifies every document by recovery route and cost:
  lightweight parser, needs docling, or reachable only by the pack that fetched
  it. Storing the text is deliberately separate from re-anchoring, so
  `reindex chunks`' orphan report is what confirms the recovered text matches
  what the passages were cut from.
- `tools/dev-postgres` now sets `shm_size: 4gb`; Docker's 64 MB default makes a
  parallel HNSW build fail with a disk-full error that is not about disk.

## 0.3.0 — 2026-08-10

Implements P0 and P1 of `docs/design/research-workflow-implementation.md`.

### Breaking

- **`PassageDraft` now requires `char_start` and `char_end`** — the passage's span
  in the document's canonical text. Every chunker must satisfy
  `draft.text == canonical_text[draft.char_start:draft.char_end]`, enforced by a
  model validator and by the contract test in
  `tests/unit/services/test_chunker_contract.py`.

  Packs that build `PassageDraft` directly must be updated:
  `marginalia-plugin-logos` (`logos/ingest/chunker.py`) and
  `marginalia-plugin-books` (`books/ingest_ia.py`). **`check_core_api` will not
  catch these**: both declare `core_api: ">=0.1.0,<1.0.0"`, which 0.3.0
  satisfies, so they load and then fail at chunk time. After updating, declare
  `requires.core_api: ">=0.3.0,<1.0.0"`.

- All core chunkers are at version `2.0`. Their output text changed
  (`prose_window` no longer collapses whitespace; `fixed_window` no longer
  strips). Passages written by the 1.0 chunkers are stale — run
  `research-engine reindex chunks`.

- `PassageRepo.keyword_search` takes `lang: str | None`. `None` searches every
  language present in the corpus.

### Fixed

- **Search filters can no longer be silently ignored.** `filter_candidate_ids`
  raises `UnsupportedFilterError` for any key it cannot translate, which makes
  `SearchResult.applied_filters` true by construction. `language`,
  `author_entity_id` and `recipient_entity_id` now have real branches;
  requesting an unregistered filter extension raises `UnknownFilterExtension`
  instead of being dropped. `similar_to` and `extract` now pass the extension
  registry, so extension filters work there at all.
- **`metadata` filter used JSON containment that compiled to a string `LIKE`**
  and matched almost nothing. Now casts to `jsonb` for a real `@>`.
- **Text search is no longer hardcoded to English.** Documents are indexed under
  a Postgres config derived from their language, defaulting to `simple` (no
  stemming) rather than `english` (wrong stemming). Keyword search unions one
  indexed branch per language present, which keeps the GIN index usable — the
  obvious `plainto_tsquery(pf.lang_config, ...)` form forces a sequential scan.
- **`index_fts` upsert now refreshes `lang_config`**, not just `ts`; previously
  re-indexing under a new language left the column describing a stemming that no
  longer applied.
- `make migrate` pointed at a nonexistent `alembic.ini`.
- `SearchFilters` with no substantive filter no longer triggers a full-corpus
  candidate scan reported as an applied filter.

### Added

- **Canonical document text** (`core.document_texts`, migration `003`) — the
  substrate passage offsets address, with a normalized copy and trigram index
  for quote verification.
- **Passage offsets** (`passages.char_start` / `char_end`, migration `004`),
  nullable until the corpus is re-anchored.
- **`research-engine reindex chunks [--document-id …] [--dry-run]`** —
  re-chunking that re-anchors `mentions`, `extractions`, `extraction_records`,
  `events.source_passage_id` and `edges.source_passage_id` onto the new passages
  before deleting the old ones. A real run is gated by a preflight pass that does
  the same work and rolls back; it aborts before writing if more than 0.5% of
  passages cannot be re-anchored.
- `IngestionClient.ingest_drafts(..., full_text=...)` — the canonical text a
  pack's draft offsets index into. Omitting it logs a warning and leaves the
  document unanchorable, since the offsets then address nothing stored.
- **`research-engine usage`** and the **`llm_usage`** MCP tool — `cost_estimate`
  has been written faithfully since the schema existed and read by nothing.
- **`BudgetGuard`** — set `RE_LLM_BUDGET_USD` to refuse LLM calls past a rolling
  spend limit. Wraps the adapter in `composition.py`, so plugins are covered too.
- `RE_DEFAULT_LANGUAGE` for corpora whose parsers do not report a language.
- `make test-integration`, `make test-all`, `make lint`, `make migrate-down`,
  `make migrate-status`.

### Notes

- Documents ingested before `003` have no canonical text, so `reindex chunks`
  skips them and lists them. Re-ingest to make them re-anchorable: reconstructing
  text from overlapping chunks would produce confidently wrong offsets.

## 0.2.0 — 2026-05-05

### Added
- `IngestionClient.find_existing(source=..., source_pattern=...)` — plugins can now look
  up already-ingested documents by exact source path or substring match without reaching
  into the corpus schema. Backed by `IngestionOrchestrator.find_existing` and stubbed in
  `DeniedIngestionClient` so denied callers fail loudly with `PermissionDenied`.

### Notes
- `IngestionClient` is a `Protocol`; adding a method is technically a breaking change for
  any out-of-tree implementations. Bundled implementations are updated.
- Plugins relying on the new method should declare `requires.core_api: ">=0.2.0,<1.0.0"`
  in `pack.yaml`. This *is* enforced at load time by `check_core_api`
  (`plugins/loader.py`) — but only against what a pack declares, so an
  open-ended specifier still admits an incompatible core.
