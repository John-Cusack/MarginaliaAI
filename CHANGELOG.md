# Changelog

### Docling's process pool is sized from measurement, and survives being wrong

Re-ingesting a 1,224-page book killed a worker on a 64 GB machine —
`Out of memory: Killed process 53655 (python) anon-rss:8238088kB` — and lost the
document ten minutes in. The same workload had already caused a laptop to power
off hard, which was attributed at the time to heat. It was memory both times.

**The constant was a guess.** `_WORKER_MEMORY_MB` was 2,048 with a comment
claiming "measured empirically at ~1.5–2GB peak per worker". Nothing had measured
it. On 32 cores and 64 GB the sizing returned 13 workers, and 13 workers at the
real cost is about 70 GB. The manual override in use at the time was 12; the
shipped default would have failed identically.

**What the measurement actually says**, converting Campaigns of Napoleon on the
server, one range per fresh process:

| range | pages | peak RSS |
|---|---|---|
| 1–25 | 25 | 5,109 MB |
| 1–50 | 50 | 5,434 MB |
| 1–100 | 100 | 5,359 MB |
| 1–200 | 200 | 5,333 MB |
| 26–50 | 25 | 3,232 MB |
| 51–75 | 25 | 3,287 MB |
| 76–100 | 25 | 3,242 MB |

Peak is **flat in page count** — eight times the pages for the same memory —
while two different 25-page ranges differ by 1.9 GB. Cost follows content
(plates, maps, tables), not volume. So total memory is simply
`workers × per-worker peak`, and the worker count is the only lever with real
leverage.

A worker's *lifetime* high-water mark is roughly twice what any single range
suggests, because it runs several: three full conversions of the book reported
**9,828**, **9,673** and **8,391 MB** — a 17% spread on the same document, since
which expensive ranges land on which worker is luck. `_WORKER_MEMORY_MB` is therefore **10,240**, and
it is deliberately pessimistic — it assumes every worker peaks at once, which
measured runs say they do not (6 × 9.7 GB predicts 58 GB against an observed
37 GB). Relying on peaks staying staggered is exactly the assumption that breaks
on a document where every range is expensive, and the failure mode is the OOM
killer. On a 64 GB machine that means 4 workers where the old code chose 13: the
book takes about six minutes longer and finishes.

The unexplained `// 2` that half-compensated for the old figure is gone, as is
the `max(2, ...)` floor that guaranteed parallelism on the machine least able to
afford it.

**Each worker builds its converter once, not once per task.** Doing it per task
made a *smaller* `pages_per_task` quietly worse, since more tasks per worker
means more model loads. Over a full book this cut steady-state memory from
37.4 GB to 34.0 GB and pulled the spread across non-peak workers from
4,373–6,162 MB down to 3,792–4,073 MB. It does not lower the peak — that is set
by one expensive range, and no amount of tidiness afterwards undoes a high-water
mark.

**Task size no longer derives from the worker count.** It was
`ceil(total_pages / workers)`, so how much one worker held was a property of the
document and no single budget could suit both a 320-page thesis and a 1,224-page
book. It is now a fixed `docling_pages_per_task`, default 50. This buys less than
expected — it does not bound memory, per the table above — but it makes the
budget a measurable constant, spreads expensive pages across workers rather than
concentrating them, and caps what a dead worker costs to redo.

**A killed worker no longer costs the document.** `BrokenProcessPool` was
unhandled; its message, "A process in the process pool was terminated abruptly",
names no cause, and attributing it the first time took reading kernel logs. It is
now treated the way `embed_batches` treats a batch too large for the accelerator:
halve and retry. Concurrency comes down first — it cannot change the output, and
it is the rung with the leverage — and task size only once one worker remains.
The raised exception now says what it means and how to confirm it in `dmesg`.

**Workers are made the preferred OOM victims.** The ladder can only run if the
process supervising it survives, and left to its own scoring the kernel may pick
the parent — it holds the whole document's text. Each worker raises its own
`oom_score_adj`, which needs no privileges, so the reaper takes something
recoverable. Without this the recovery path is a coin flip on which process dies.

The boost is **relative to the score the worker inherited**, clamped to the
kernel's ceiling of 1000. An absolute value expresses no preference inside a
container that already places the whole process tree above zero — CI runs at 500,
where setting a worker to 500 is a no-op that reads like a working safeguard.

**Retries keep the work that survived.** One dead worker breaks the executor for
everything pending, but ranges that already finished are still good; in the
failure this was written for, eleven of twelve workers had completed and all of
it was discarded. Results are now keyed by page range and carried into the next
attempt, so a retry converts only what is missing — and they are dropped when the
task size changes, because a different split is a different set of ranges.

**It reports what it cost.** Each worker returns its own `ru_maxrss`, logged as
`peak_worker_mb` at INFO alongside the budget it is meant to predict, and stored
in the document's `metadata["conversion"]` with the halving count — the role
`BackfillReport.halvings` already plays for `embedding_batch_size`. The sizing
decision itself moved from `logger.debug` to `logger.info`; at DEBUG, none of the
numbers that mattered were printed during the run that ran out of memory.

**`docling_device` now does something.** It was declared in settings and read
nowhere, while the converter looked at `RE_DOCLING_DEVICE` itself. `DoclingModule`
was the only component the composition root did not configure; it now takes
`device`, `max_workers` and `pages_per_task`, joined by `RE_DOCLING_MAX_WORKERS`
and `RE_DOCLING_PAGES_PER_TASK`. The device is also part of the converter cache
key, which it was not — the first converter built decided the device for every
later conversion in the process.

**No re-ingest.** Splitting a PDF differently produces byte-identical canonical
text: 1×100, 2×50 and 4×25 pages all yield the same 244,294 characters, asserted
now in `tests/integration/test_docling_conversion.py`. That file is also the first
test ever to run a real PDF through this path. Converting the whole 1,224-page
book at the new defaults reproduces what the corpus already holds exactly —
2,907,621 characters and 1,211 page markers — with no halvings.







### Docling headings: drop the front matter, rejoin the split ones

Docling sets a dedication, a copyright line and a calligrapher's credit like
headings and detects them as headings, so a passage on page 3 cited itself as
belonging to `"Donated In Memory Of ROBERT EDWARD PATOW"`. Layout also splits one
heading across lines and reports each line as its own item, leaving a sibling
node that is a fragment — `COMMAND IN THE WESTERN` and then `THEATER`.

- **Headings before the table of contents are dropped.** Docling's own
  `document_index` label is the cut, and it is the *first* such item, not the
  last: a contents list runs over several pages with headings interleaved, and
  cutting at the last one takes real sections like `APPENDICES` with it. Only the
  heading goes — the text stays in the canonical text and belongs to the node
  above. A document with no detected contents page keeps every heading, because
  there is nothing to cut against.
- **Adjacent headings are merged** when only whitespace separates them. Whether
  a heading arrives split is not stable between Docling runs, so this is a repair
  rather than a preference.

`content_layer` was tried first and is no help: Docling marks a dedication and a
chapter alike as `ContentLayer.BODY`, with no furniture classification at all.

- **Right-set text is not a heading.** Docling's layout model reads visual
  salience, so an isolated short line is a heading whether it is a chapter title
  or the closing of a letter: `"Yours affectionately Geo B McClellan"` became a
  node, and passages beneath it cited themselves as belonging to a signature.
  Alignment separates them, and the test is simple because of how alignment
  works — a left-aligned heading starts at the margin, a centred one of width
  `w` on a page of width `W` starts at `(W - w) / 2`, which is left of `W / 2`
  for any width. Only text set to the right begins past the midpoint. Measured
  on the McClellan papers, every genuine heading starts between 5% and 14%
  across and the closing starts at 57%. It falls open rather than closed: a
  document with no geometry keeps every heading.

**Still not fixed:** an address line set flush left — `"SLM B Esq"` under
`"To Samuel L. M. Barlow"` — is geometrically identical to a real heading. Only
semantics separates those two, and no rule here can.

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
