# Changelog

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
