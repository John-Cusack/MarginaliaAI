# Rust Rewrite Order — MarginaliaAI

Goal: full Python → Rust, starting where Rust pays most and breaks least.
Rule: pure / deterministic / CPU-bound / high-call-volume first; LLM / network /
DB / dynamic-plugin / framework-bound last.

Each phase is a Rust crate with a PyO3 seam (`marginalia_rs.*`) exposing the
same function signatures as the Python it replaces. Cut callers over only after
property tests pass on corpus fixtures (Python vs Rust byte-identical).

```
Phase 0 (types) ──► Phase 1 (text proof) ──► Phase 2 (chunk/windows/fusion)
      │                       │                           │
      └─────────► Phase 3 (parsers) ──► Phase 4 (works pure core)
                                                      │
                          Phase 5 (retrieval SQL/words) ◄─┘
                                      │
                    Phase 6 (IO adapters) ──► Phase 7 (framework last)
```

## Phase 0 — Shared kernel (first, unlocks everything)

Pure data model. No IO. Every later crate depends on it.

| Rewrite | Python source | LOC | Rust shape |
|---|---|---|---|
| Span / passage / doc DTOs | `packages/sdk/src/research_engine_sdk/types.py` (363), `packages/core/.../domain/spans.py` (31), `domain/passages.py` (164), `domain/documents.py` (70), `domain/nodes.py` (207), `domain/citations.py` (95), `domain/claims.py` (219), `domain/works.py` (228), `domain/edges.py` (39), `domain/provenance.py` (154), `domain/common.py` + `domain/errors.py` (260) | ~1,800 | `marginalia-types`: `serde` structs, `thiserror` error enum. Replaces `pydantic.BaseModel` |
| Ports (traits) | `packages/core/.../ports/repositories.py` (14.5KB), `ports/embedding.py`, `ports/reranker.py`, `ports/llm.py`, `ports/http.py`, `ports/clock.py` | small | Rust `trait` definitions; Python adapters implement them behind the seam until Phase 5/6 |

Why first: zero behavioral risk, unblocks all later phases. No perf win — enables them.
Acceptance: `serde_json` round-trip fixtures from current Pydantic models pass.
Status (2026-09-18): DONE as `crates/marginalia-types` — 38 tests green,
100% lines/functions/regions (`cargo llvm-cov`), zero clippy warnings, `cargo fmt` clean.
27-model bidirectional Python↔Rust parity proven (throwaway since removed).

## Phase 1 — Text proof core (highest value / lowest risk)

Hottest pure path. Every `verify_quote`, `claim_upsert`, `work_validate`,
ingest re-anchor calls this. Unicode + offset-map bugs are the current
correctness risk; Rust's `char` boundaries + `icu_normalizer` fix the class.

| Rewrite | Python source | Symbols |
|---|---|---|
| Normalization + offset maps | `services/text/normalize.py` (150) | `normalize`, `normalize_with_map`, `normalize_for_matching`, `_linebreak_hyphen_deletions`, `NORMALIZATION_VERSION` |
| Whitespace collapse + substring index | `services/text/anchoring.py` (121) | `Span`, `overlap`, `collapse_whitespace_with_map`, `CanonicalIndex`, `best_overlap` |
| Token estimates | `services/text/tokens.py` (52) + `packages/sdk/.../chunking.py` (151) | `chars_per_token`, `min_chars_per_token`, `approx_tokens`, `token_budget_chars`, `trim_span`, `split_at_boundary`, `cap_spans` |
| Quote-tier decision (pure part only) | `services/verification/quote.py` (498) — pure core ≈ 200: `_find_folded`, divergence slicing, `Tier` decision, `DEFAULT_NEAR_THRESHOLD`, `MAX_CANDIDATES`, `DIVERGENCE_CONTEXT` | `Tier::{Exact,Normalized,Near,NotFound}`, `QuoteVerification`, `Divergence` |
| Section splitting | `services/text/sections.py` (213) | section boundary detection used by structural chunking + windows |
DB-bound shell (`QuoteVerifier` candidate lookup via trigram index, passage repos)
stays Python in this phase; only the string core moves behind `marginalia-text`.
Why second: called per-citation, per-chunk; 1:1 property-testable
(`normalize_with_map` index map must round-trip on WLC + TDNT + Louw-Nida fixtures).
Acceptance: byte-identical `(normalized, index_map)` and tier on quote fixtures.
Status (2026-09-18): DONE as `crates/marginalia-text` — 43 tests green
(42 from Phase 1 plus the `split_span` equivalence test Phase 2 added to pin
the pre-validated path the chunkers route through),
100% lines/functions/regions, zero clippy warnings, `cargo fmt` clean.
Differential vs CPython 3.13: 26 normalize cases (incl. `\w` edges) + 300
seeded token strings + spans/sections + 13 quotes through the real
`QuoteVerifier` all bit-exact; `is_space`/`is_word_char` swept over all
1,114,112 code points (see Parity findings).

## Phase 2 — Chunking, windows, fusion (pure retrieval math)

Deterministic, no network, easy to fuzz.

| Rewrite | Python source | Symbols |
|---|---|---|
| Chunkers | `services/ingestion/chunking/fixed_window.py` (79), `prose_window.py` (159), `structural.py` (188), `whole_or_paragraph.py` (114), `services/ingestion/offsets.py` (170) | `trim_span` consumers, `prose_window` sentence join, structural node split |
| Read windows | `services/search/windows.py` (274) | `choose_window`, `WindowPlan`, `WindowSource`; p50/p90-tuned min/budget logic |
| Fusion | `services/search/fusion.py` (67) | `rrf_fuse` (`RRF_K=60`), `weighted_fuse` |
| Filter/lang helpers (pure part) | `services/search/filter_extensions.py` (128), `langconfig.py` (71), `hit_source.py` (62) | `pg_config`, filter-pushdown predicate building |

`services/search/hybrid.py` (249, `HybridSearchService::find_passages`) orchestration
— `asyncio.gather` over embedding + `vector_search` + `keyword_search` — stays
Python until Phase 5; it calls into this crate for fusion/windowing.
Acceptance: RRF/weighted scores + window spans identical on recorded search fixtures.
Status (2026-09-19): DONE as `crates/marginalia-chunk` — 54 tests green,
100% lines/functions/regions (`cargo llvm-cov`, all three crates), zero clippy
warnings, `cargo fmt` clean. Differential vs CPython 3.13 (throwaway, since
removed): 157 chunk cases over every `CONTRACT_TEXTS` shape × config matrix
(incl. `k=7`-ignored quirk, falsy page/level/heading, repeated-section cursor,
oversized-section windows, metadata carry-through), 26 sentence/paragraph
scanner cases, 44 fusion cases (bit-exact incl. the 1-ulp `1/65` accumulation),
11 `choose_window` + 5 budget + 6 `build_window` cases (incl. negative budgets),
full `KNOWN_CONFIGS` table — all bit-exact. Deliberate non-ports (no new pure
logic): `offsets.py` (span-less recovery is `CanonicalIndex::find` +
`collapse_whitespace`, already in `marginalia-text`), `hit_source.py` (one
conjunction, no predicate to port), `filter_extensions.py` (`build_clause` is
SQLAlchemy — Phase 5). Known typed-boundary deviations on malformed input only:
negative node offsets clamp to 0 (`Span` is `usize`), `weighted_fuse` exact ties
keep vector-then-keyword order (Python iterates a `set`, order varies run to
run — scores per id verified instead). New parity finding: `serde_json`
mis-rounds some ≥17-digit decimals by 1 ulp on parse (proven: `repr(1/65)`);
floats crossed the differential as exact bit patterns. Seam work that moves
scores as JSON must account for this.

## Phase 3 — Document parsers (throughput win)

CPU-bound parsing currently in Python (`BeautifulSoup`/`lxml`/`ebooklib`).
Each parser returns SDK `ParsedDocument`; interface already stable.

| Rewrite | Python source | LOC | Rust shape (as built) |
|---|---|---|---|
| Plain text | `modules/plain_text.py` | 77 | `encoding_rs` + existing span logic (`plain_text.rs`, `pystr.rs`) |
| Markdown | `modules/markdown.py` | 141 | hand port of the regex pipeline (`markdown.rs`; `pulldown-cmark` rejected — the module strips formatting, it does not render) |
| HTML | `modules/html.py` | 190 | `scraper` + entity pre-encoding (`html.rs`, `normalize_entities.rs`, `html_entities.rs`, `case_tables.rs`; `lol-html`/`kuchiki` rejected — fragment/body semantics need a real tree) |
| EPUB | `modules/epub.py` | 199 | `zip` + `scraper` + shared `xml.rs` (`epub.rs`) |
| TEI XML | `modules/tei_xml.py` | 241 | `quick-xml` 0.39 (`xml.rs`, `tei.rs`) |
| PDF text layer | `modules/pdf_text.py` | 107 | `lopdf` 0.42 + `pdf-extract` 0.12, rules ported exactly, page text is the engines' (`pdf.rs`) |
| One-off corpus scripts | `scripts/bible_layout.py` (265), `scripts/wlc_extract.py` (437), `scripts/ingest_wlc.py` (230), `scripts/load_versification.py` (329), `scripts/discover_overlaps.py` (266) | ~1,500 | throwaway Rust binaries `src/bin/` (byte-identical outputs, no permanent tests) |

Explicitly NOT in this phase: `modules/docling_converter.py` (1,095) — wraps the
Docling ML model; keep the Python shim, port only its post-processing if profiling
justifies it. `scripts/scrape_kindle.py` (33, Playwright) never ports.
Acceptance: `ParsedDocument.text` + `structural_locators` byte-identical on the
corpus-setup fixture set (`docs/corpus-setup.md`).
Status (done 2026-09-19): `cargo test --workspace` 283 green (38 types + 43
text + 54 chunk + 148 parse); `cargo llvm-cov --lib --tests -p marginalia-parse`
100% lines/functions/regions; zero clippy; `cargo fmt` clean. Permanent suite
`crates/marginalia-parse/tests/parse.rs` mirrors the Python parser suites
case-for-case; throwaway differential (102 fixtures: text, sections, title,
metadata, detect scores as bit patterns) byte-identical except PDF page text
(see findings); throwaway deleted. `detect` takes raw head bytes where the
module reads strict (`plain_text`, `markdown`), decoded heads where it reads
`replace` (`html`, TEI); EPUB/TEI/PDF `detect` take bytes. No PyO3 seam (no
consumer yet); `docling_converter` and `scrape_kindle` untouched per scope.

## Phase 4 — Works authorship pure core (correctness win)

Deterministic state machine + hashing. Rust types (`frozen` vs `draft`,
`GateName`) enforce what today is convention. DB-touching service shells stay
Python behind traits until Phase 5.

| Rewrite | Python source | LOC | Notes |
|---|---|---|---|
| Content hash | `services/works/hashing.py` | 107 | `compute_content_hash`, `canonical_json` — `sha256`, trivial, port early inside Phase 4 |
| Markers / citations / assembly / render | `markers.py` (37), `citations.py` (79), `assembly.py` (168), `render.py` (100), `files.py` (164), `drafting.py` (538, pure part) | ~1,100 | `find_markers`, `assemble_revision`, `hash_assembled`, markdown render |
| Verify / validate rule engine | `services/works/verify.py` (372), `validate.py` (663) | ~1,000 | `WorkVerifier` + `WorkValidationService` pure rules (`AUTH_*` ids, `_NARROW_INTENTS`, `_GROUNDED_TYPES`, gate `none`/`freeze`/`publish`); repo calls stay behind `Ports` traits |
| Trace / attach / publication | `trace.py` (327), `attach.py` (312), `publication.py` (163) | ~800 | grounding tree, link resolution |
| Date parsing | `services/text/dates.py` | 446 | pure; used by extraction/works, port with `chrono` + `icu_calendar` |
Explicitly NOT in this phase: `services/works/cite.py` (141) and
`services/works/work_service.py` (398) — DB writers/transactions; they stay
Python behind `Ports` traits until Phase 5, as do all repo calls inside
`verify.py`/`validate.py`/`trace.py` (port the pure rules only).

Acceptance: frozen-hash stability (`AUTH_REVISION_MUTATED` fixtures) + gate reports
identical on `works/mishpat-tsedaqah-survey.md` and `tests/` work fixtures.
Status (done 2026-09-20): `cargo test --workspace` 744 green (38 types + 43
text + 54 chunk + 148 parse + 460 works + 1 types-ports); `cargo llvm-cov
--lib --tests -p marginalia-works -p marginalia-types` 100%
lines/functions/regions on both crates (text/chunk/parse intact at 100%:
parse lib+tests; its `src/bin/` throwaways stay uncovered by design);
zero clippy; `cargo fmt` clean. Permanent suite is in-module `#[cfg(test)]`
per file, mirroring the work/date suites case-for-case; throwaway
differential (frozen-hash hex incl. copy-id stability, gate reports on the
survey fixture + seeded edges + all 182 mishpat citations, render, finder)
bit-exact, then deleted. No PyO3 seam (no consumer yet); `cite.py` and
`work_service.py` untouched per scope.
## Phase 5 — Retrieval storage + words (first DB-touching phase)

Port only after the pure cores are stable, because this binds `sqlx` + Postgres
extensions (`vector`, `pg_trgm`, `ltree`).

- `adapters/storage/postgres/repositories/passages.py`, `document_texts.py`, `nodes.py`, `spans.py`, `documents.py` — `vector_search`, `keyword_search`, `filter_candidate_ids`, window/ancestor reads.
- `services/words/lookup.py` (409, `LemmaQuery`, `LemmaResult`, `MAX_OCCURRENCES=2000`) + versification map (`migrations/016_versification.py`, `014_words.py`, `015_words_language_strong.py`) — verse-identity (never char-span) logic ports with the SQL.
- `services/ingestion/pipeline.py` (170), `orchestrator.py` (410), `structure.py` (226), `reindex.py` (575), `embed_batches.py` (121), `text_backfill.py` (206), `embedding_backfill.py` (259) — orchestration ports last inside this phase, calling Phase 2/3 crates.
- `eval/runner.py`, `metrics.py`, `queryset.py` + `services/argument/claims.py` (227), `context.py` (100) — pure scoring/context assembly; move with this phase as consumers of retrieval.

Acceptance: `tests/integration` search/words suites pass against the Rust repos.
Status (done 2026-09-20): `cargo test --workspace` 951 green (744 + 207
`marginalia-ret`: 169 pure-port unit tests mirroring the words/filter/eval/
argument/ingest suites case-for-case, 37 repo-surface pins, 1 integration
driver with 20 scenarios in `tests/pg_repos.rs` incl. deterministic fault
injection per fallible boundary); `cargo llvm-cov --lib --tests
-p marginalia-ret` 100% lines/functions/regions (7414/586/5119); zero
clippy; `cargo fmt` clean. Throwaway differential (seed corpus incl.
multilingual keyword, trigram edges, lemma caps/partials/qere,
versification joins, span-race convergence, reindex idempotence; scores and
vectors as bit patterns) green then deleted with fixtures. Python
`tests/integration` search/words/spans/reindex/backfill suites pass
unchanged against real Postgres (one pre-existing data-drift failure in
`test_versification.py::TestSchemesCoverEveryEdition::
test_every_edition_names_a_versification_scheme` — unmapped lexicon/article
editions ingested since the map load, untouched by this phase). Pip checks
green (`pytest packages/sdk/tests`, `uv build`, `twine check --strict`,
wheel import smoke). No PyO3 seam (no consumer yet). Load-bearing
decisions: single-statement merges where sequential same-table reads left
error arms no deterministic fault can fire (`update_metadata`
`UPDATE...RETURNING`, passage/document inserts, span resolve
`ON CONFLICT DO UPDATE...RETURNING`, `get_context` marked `UNION ALL`,
bulk `unnest` writes, aggregates loop with derived totals/books);
`FilterValidation` typed errors replacing message-prefix sniffing;
`counts={}` (not five empty arrays) on the zero-result wire shape;
`TEMPLATE` scratch copies banned (crashed dev Postgres on 3.7 GB copy —
schema-replay + `COPY` instead); pooled connections roll back before
propagating mid-transaction failures (ef-search path included).

## Phase 6 — IO adapters (thin, port for uniformity, no perf win)

Status (done 2026-09-21): `cargo test --workspace` 1181 green (975 excl. `marginalia-ret`: 38 types + 43 text + 54 chunk +
148 parse + 460 works + 1 types-ports + 231 new `marginalia-io`; ret lib 206 green; `tests/pg_repos.rs` compiles clean but
needs live Postgres — unrunnable in this environment); `cargo llvm-cov --lib --tests -p marginalia-io` 100%
lines/functions/regions (9074/670/5505; other crates untouched at 100%); zero clippy; `cargo fmt` clean. Permanent suite is
in-module `#[cfg(test)]` per file, mirroring the embedding/budget/schemas/validation/routing/rerank suites case-for-case;
throwaway differential (recorded Python wire bytes incl. `1/65` + `-0.0` + 17-digit floats as bit patterns, LLM request/response
cassettes for both providers, rank order, budget messages, non-ASCII anchors, output-schema, resolve table) bit-exact, then
deleted with fixtures. No PyO3 seam (no consumer yet). Pip checks green (`pytest packages/sdk/tests`, `uv build`, `twine
check --strict`, isolated wheel `import *` smoke — note: the package exposes `__all__`, there is no `research_engine_sdk.all`
module; the smoke imports the recorded intent). Scope deltas vs the table: `local_bge` inference, `executor`/`postprocess`/
`registration` orchestration, and `build_inference` wiring stay Python (weights/process/framework — Phase 7); routing ports
`resolve_mode`/fallback-once/summary-strings/`Workload` only.

Ported (all in `marginalia-io`, reqwest + tokio first bound here):
- `adapters/embedding/{wire,server,remote_api}.py` + `cli/embed_server.py` — `wire.rs` (byte-exact models), `embedding.rs`
- (`RemoteEmbeddingClient`, handshake-once, circuit 3, 120 s/10 s timeouts), `embed_server.rs` (409/404/503 decisions, banner,
- defaults); inference weights stay out-of-process.
- `adapters/reranker/{remote_api,scoring,noop}.py` + `adapters/http/httpx_adapter.py` + `adapters/inference/routing.py` —
- `reranker.rs` (circuit 2, 30 s timeout-first taxonomy, `rank_from_scores`, sync `NoopReranker`), `http.rs` (monomorphic
- `HttpCore`, `HttpAdapter` 30 s default), `routing.rs` (pure decisions).
- `adapters/llm/{anthropic,openai_compatible,budget_guard}.py` — `llm.rs` (reqwest REST, exact bodies/tools/costs/drafts/taxonomy),
- `budget.rs` (cache math + refusal boundary; async shell stays Python).
- `services/extraction/{schemas,validation}.py` + `services/entities/service.py` + `services/events/service.py` — `schemas.rs`,
- `validation.rs`, `entities.rs` (`upsert_decision`, alias rules), `events.rs` (bucketing) — pure shaping only.

## Phase 7 — Framework last (port the portable core, record the rest)

Status (done 2026-09-21, rebased + drift-followed 2026-09-22): `cargo test
--workspace --exclude marginalia-ret` 1051 green (975 + 35 new
`marginalia-history` + 39 new `marginalia-io` settings/catalog + 2 new
`marginalia-types` drift pins); ret lib 207 green, `tests/pg_repos.rs`
compiles clean (incl. the new `find_by_edition_id` scenario) but needs live
Postgres — unrunnable in this environment; `cargo llvm-cov --lib --tests
-p marginalia-history -p marginalia-io -p marginalia-types` 100%
lines/functions/regions on all three (other crates untouched at 100%); zero
clippy; `cargo fmt` clean.
Permanent suites are in-module `#[cfg(test)]`, mirroring the history,
config, config-resolution, and dispatch suites case-for-case; throwaway
differentials (36-name surname battery, holdings sequences, cadence reports
across month/day/week bins incl. week-00 edges, gap rhythm with confidence as
bit patterns, 22-value `round` battery; settings-resolution matrix over real
tmp projects, 35-report describe matrix, catalog/envelope round-trips incl.
the `['fast', 'slow']` Python-repr message) bit-exact, then deleted with
fixtures. No PyO3 seam (no consumer yet). History/config Python suites
(58 tests) pass unchanged. Pip checks green (`pytest packages/sdk/tests`,
`uv build`, `twine check --strict`, isolated wheel `import *` smoke — 82
exports from the built wheel).

Ported:
- History pure core, new crate `marginalia-history` (`holdings.rs`:
- `surname`, `direction_of`, `Holdings` + coverage caveat; `cadence.rs`:
- time-bin parsing, `%Y-%m`/`%Y-W%W`/`%Y-%m-%d` buckets, direction split,
- timeline/anomalies/summary, median-interval gap rhythm with the 14-day
- floor, `count_by_method`, `RESOLVED_SUFFIX`, exact half-even `round1`).
- `config/settings.py` pure rules, into `marginalia-io` (`settings.rs`:
- `RE_` names, 35-field defaults/declaration order, literal validators,
- derived dirs/URLs/works refusal, env-file discovery, key parsing, source
- attribution, secret masking, load precedence).
- MCP SDK-independent shaping, into `marginalia-io` (`catalog.rs`:
- 42-name registry in dispatch order, `ToolCatalog` core/pack/version rules,
- `_validate_input` with Python-repr enum messages, `envelope`/`failed`/
- `unknown_tool`/`validation_error`/`permission_denied`, dotted/underscored
- id matching).

Keep-Python decisions (one-line rationale each, recorded not silent):
- `plugins/discovery.py`, `loader.py`, `registry.py`, `activation.py` —
- `entry_points`/`importlib` loading + manifest-hash audit; `dlopen` buys
- nothing until plugins are Rust-native.
- `plugins/permissions.py` (+ `Denied*`/`Gated*` clients) — sandbox gates
- over live SDK clients; the denial *shape* ports (`catalog.rs`), the gates
- stay where the clients live.
- `plugins/compatibility.py` — PEP 440 semantics owned by `packaging`,
- plugin-system-adjacent, no Rust consumer.
- `mcp/server.py` — the `mcp` SDK stdio transport; `rmcp` only after all
- tool backends are Rust.
- `mcp/dispatch.py` SDK-bound parts — `_select_clients`
- (`inspect.signature` over live callables), `_register_all` closures,
- `_build_*_entries` (SDK `Tool` objects + live container/loader),
- `_make_core_handler` (validate-call-envelop over live handlers; its
- constructors ported).
- `mcp/tools/*` handlers + schemas — service-bound; schemas reference live
- registries (`find_passages` filter extensions).
- `cli/*` (all 17 groups: work, reindex, plugin, extraction, embeddings,
- embed-server, search, usage, verify, eval, doctor, backup, config, ingest,
- database, serve, main/status) — every command dispatches into Python
- services through the container; a consumerless `clap` shell proves nothing.
- `config/settings.py` binding (`Settings`, `load_settings`) —
- `pydantic-settings` framework; the rules it enforces ported.
- `composition.py` + `runtime.py` — the seam itself; dissolves as phases
- land, not ported.
- `testing/corpus.py`, `testing/database.py`, `testing/chunker_contract.py`
- — test harness + history; never a port target.
- `find_missing_letters.py` orchestration (`tool_handler`, `_referenced`),
- `_holdings.build`, both `tool_handler` entries, `_as_uuid`/`_as_datetime`
- — live SDK-client queries + MCP-string validation glue at that boundary.
- `modules/docling_converter.py`, Playwright/Chromium flows,
- `sentence-transformers` weights, Alembic migrations — process/model/
- history boundaries, never ports (unchanged from scope).

Scope deltas vs the table: `compatibility.py` explicitly kept (was listed in
the surface without a verdict); the 42-name registry pins dispatch order
rather than duplicating descriptions/schemas SDK-side.

Rebased onto `origin/main` (0.6.2 family) before merge: 15 commits of drift,
all assessed. Contract follows ported in this branch: `edition_id` on
`Document`/`DocumentDraft` (position before `metadata`, absent reads `None`),
`PluginContext.database_url` (plaintext `Option<String>` like
`get_secret_value()`, `Debug` redacted — Python masks only the JSON dump and
no ported path dumps a context), `DocumentRepo::find_by_edition_id`
(caller's-tx read, `LIMIT 1`) with the ret impl + pg scenario, and
`EditionRepo::upsert_key(..., lock: bool)` (trait-only — ret has no edition
repo). The `pdf` parser stays at version 1.0 deliberately: 1.1 means the new
first-page identity ran, and bumping the stamp without the behavior would
suppress needed re-parses. Tracked follow-ups, each phase-sized with its own
differential (suites named): the embedding retry-smaller-batch protocol
(`wire.py` consts, server 503-vs-500 split, client 409/status taxonomy —
mirror `tests/unit/adapters/test_remote_embedding.py`); `identifiers.py`
(`normalize_doi`/`normalize_isbn`, `first_page_text`,
`with_first_page_identity`) + pdf 1.1 + the orchestrator rework
(`_store_file_document`, `_record_edition` — mirror
`test_document_identifiers.py`, `test_pdf_identity.py`,
`test_ingest_work_identity.py`).

```
Cargo.toml                  workspace: crates/marginalia-{types,text,chunk,parse,works,ret,io,history}
crates/marginalia-types/   Phase 0   serde DTOs + traits (ports) + wire helpers
  src/{lib,errors,common,spans,documents,passages,nodes,citations,claims,
       works,works_files,works_ports,edges,entities,events,extractions,provenance,
       sdk,ports,wire}.rs
  tests/kernel.rs           39 contract tests (wire values, validation, serde)
crates/marginalia-text/    Phase 1   normalize, anchoring, tokens, spans,
                                      sections, quote-tier, word_table
  src/{lib,chars,normalize,anchoring,tokens,spans,sections,quote,word_table}.rs
  etc/gen_word_table.py     regenerates word_table.rs from the running Python
  tests/text.rs             43 parity tests (mirrors the Python suites)
crates/marginalia-chunk/   Phase 2   chunkers, read windows, fusion, langconfig
  src/{lib,fixed_window,prose_window,structural,whole_or_paragraph,
       windows,fusion,langconfig}.rs
  tests/chunk.rs            54 parity tests (mirrors the chunk/window/fusion
                              suites + the chunker contract)
crates/marginalia-parse/    Phase 3   document parsers (pure cores + detect)
  src/{lib,plain_text,markdown,html,normalize_entities,html_entities,
       case_tables,epub,tei,xml,pdf,pystr}.rs
  src/bin/{bible_layout,wlc_extract,ingest_wlc,load_versification,
       discover_overlaps}.rs   one-off corpus scripts (throwaway binaries)
  etc/{gen_case_tables,gen_html_entities}.py   regenerates the tables above
  tests/parse.rs            148 parity tests (mirrors the parser suites)
crates/marginalia-works/    Phase 4   works pure core (hashing → dates)
  src/{lib,hashing,markers,files,citations,assembly,drafting,render,verify,
       validate,trace,attach,publication,dates,printable_table}.rs
  etc/gen_printable_table.py regenerates printable_table.rs from the running
                              Python (repr printability, like gen_word_table.py)
  in-module #[cfg(test)]     460 parity tests (mirrors the work/date suites;
                              no tests/ dir — one crate, no merge surface)
crates/marginalia-ret/      Phase 5   retrieval storage + words (sqlx repos)
  src/{lib,words,filters,eval,argument,ingest}.rs   pure builders + scoring
  src/repos/{repos,documents,texts,passages,spans,nodes,lemma}.rs
                              `sqlx` repos implementing the `marginalia-types`
                              port traits (owned `PgTx`, `::vector`/`::ltree`/
                              `::timestamptz` casts, `RETURNING`/`unnest` writes)
  tests/pg_repos.rs          1 ordered integration driver, 20 scenarios incl.
                              deterministic per-boundary fault injection
                              (scratch DBs via schema-replay + `COPY`, dropped
                              after every green run)
crates/marginalia-io/      Phase 6+7 IO adapters + framework shaping (reqwest clients + pure shaping)
  src/{lib,errors,budget,wire,embedding,embed_server,reranker,routing,
       http,llm,schemas,validation,entities,events,settings,catalog}.rs
  in-module #[cfg(test)]     269 parity tests (Phase 6 suites + Phase 7
                              settings/catalog suites; same pattern: no
                              tests/ dir, no merge surface)
crates/marginalia-history/ Phase 7   history-plugin pure core
  src/{lib,holdings,cadence}.rs
  in-module #[cfg(test)]     35 parity tests (mirrors the holdings/cadence
                              suites; no tests/ dir — one crate, no merge
                              surface); depends on marginalia-text for
                              `is_space`/`strip`/`is_word_char`
Future crate (`-cli`) joins the workspace the same way. No PyO3 seam yet: all
eight crates are standalone libraries verified by differential, not wired into
callers — the seam lands with the first consumer (Phase 6 adapters calling
`marginalia-ret` for search, or framework wiring against Phase 7 shaping).
Check: `cargo test --workspace --exclude marginalia-ret` (1051 green lib/unit:
41 types + 43 text + 54 chunk + 148 parse + 460 works + 270 io + 35 history;
ret lib 207 green separately; `tests/pg_repos.rs` compiles clean but needs
live Postgres — unrunnable in this environment), `cargo llvm-cov --lib --tests
-p marginalia-history -p marginalia-io -p marginalia-types` (100%
lines/functions/regions on all three; earlier crates intact), `cargo clippy
--workspace --all-targets` (zero), `cargo fmt --check` (clean).

## Parity findings (load-bearing; re-verify if the Python upgrades)

Learned porting Phases 0–5 against CPython 3.13 (Unicode 15.1). Each is pinned
by a permanent test; the throwaway differentials that proved them are gone.

- `sum()` of floats is Neumaier-compensated since 3.12 — a naive fold differs
- by 1 ulp, flippable through `int()` truncation in `approx_tokens`. Rust
- mirrors the compensation (`tokens::neumaier_sum`, proven bit-exact over
- 20,000 randomized inputs). Any rate/estimate change re-runs the seeded
- token battery in `tests/text.rs`.
- Python `re \s` (str) == `str.isspace()` exactly (same 10 ranges, all BMP),
- and both exceed Unicode White_Space by U+001C–U+001F. The `regex` crate's
- `\s` misses those four, so every pattern spells
- `[\p{White_Space}\p{Z}\x1c\x1d\x1e\x1f]` (`normalize::PY_WS_CLASS`, shared
- with sections). Pinned by range-boundary probes in `tests/text.rs`.
- Python `re \w` (str) differs on 1,710 BMP points (`No` numerics like ² count;
- `M` marks and join controls don't) — no property expression reproduces it.
- Baked as 749 ranges (`word_table.rs` + `etc/gen_word_table.py`, swept over
- all 1,114,112 code points). The linebreak scanner matches `(\S)` and
- re-scans past rejections exactly like the engine advances. Regenerate only
- on Unicode-data upgrades; any table-size change fails the pinned count.
- `bytes` fields cross JSON as UTF-8 strings on both sides and both sides
- refuse non-UTF-8 (`wire::bytes_string`, mirroring `PydanticSerializationError`).
- Raw sha256 hashes therefore never cross as JSON — storage (`bytea`) only.
- `ExtractionSchema` dumps as `schema_def` (field name, not alias): Rust
- accepts both, emits `schema`.
- Dead code removed with proof, not coverage waivers: `_join_continuing_runs`
- (join condition contradicts the split rule), `_longest_prefix` zero-guard,
- float-positivity guards, unreachables in `median_gap`/span guards. If the
- Python changes these paths, the Rust must be re-examined, not re-covered.
- `serde_json` (1.0.151) decimal→`f64` mis-rounds some ≥17-digit decimals by
- 1 ulp (proven: `repr(1/65)` parses to `...81f`, correctly rounded is
- `...820`). Python-printed floats must cross as exact bit patterns, not
- decimals, wherever bit-exactness matters — scores especially. Seam work
- that moves scores as JSON must account for this.
- Python `//` floors, Rust `/` truncates: window centering uses `div_euclid`,
- identical for real budgets, exact for every `i64`.
- `weighted_fuse` iterates a `set`: exact ties order arbitrarily (varies run
- to run under hash randomization) and cannot be byte-compared — scores per
- id are the contract there. `rrf_fuse` threads a `k` it never reads (divisor
- is `RRF_K`); Rust keeps the ignored parameter so the seam cannot silently
- "fix" scores. Pinned by the `k=7` differential case.
- Dead code removed with proof, not coverage waivers (Phase 2): the prose and
- whole-or-paragraph `split_at_boundary(...)?` `Err` arms (spans built from
- the text they split, budgets floored at 1 — routed via the infallible
- `spans::split_span` with the contract in `debug_assert`s) and the prose
:- final `if window` guard (non-blank text provably yields spans, the loop only
:- pushes). If the Python changes these paths, the Rust must be re-examined.
- Dead code removed with proof, not coverage waivers (Phase 3): the EPUB
- chapter `html_source` double evaluation (same bytes, same pure function —
- hoisted to one call), the empty-href guard (any name unquoting to `""`
- fails its manifest read first), the `dc` emptiness guard (the tree never
- holds an empty text run — `push_text` drops those), `ncx_node`/`attach`/
- `chapter_text`/`heading_from_markup` `Result`s (no fallible op inside),
- `encoding_label`'s `get(5..)` (`starts_with(b"<?xml")` proves five bytes),
- the ASCII `from_utf8` error (the `>= 0x80` scan precedes it), and the Text
- event resolve error (quick-xml 0.39 surfaces every `&`-led reference as
- `GeneralRef` or fails the read — probed — so text runs never hold one).
- If the Python or these crate versions change these paths, re-examine.
- `ebooklib` yields no TOC entries for a missing/empty `navMap`, a nav
- without a toc list, or an empty label — chapters fall back to their own
- headings. The first port errored on all three; probes corrected it to
- empty tables. A toc list *without its list* fails upstream (its own
- `AttributeError`), so that failure stands — as does a page-list without
- its list.
- `pdf-extract` 0.12 panics (unwinds) on malformed content — unknown fonts,
- dangling references — where `fitz` substitutes and succeeds. `extract_pages`
- runs behind `catch_unwind` and answers `Error::Parse` (known boundary:
- `badfont.pdf` reads `'Hi'` under fitz, errors in Rust). Its `Err` is the
- same `Document::load_mem` the caller already accepted, hence unreachable
- past it. Page *text* is the engines': `fitz` wraps long lines and trims
- page edges, `pdf-extract` does neither — assembly rules (verbatim join,
- blank-page drop, metadata-first title else first line ≤ 300 chars) are
- byte-identical over the same page texts (proven by `rs_asm`), page texts
- themselves differ (3 fixtures evidenced). Re-verify if either engine upgrades.
- `libxml2` in this environment rejects `latin-1` (`Unsupported encoding`);
- single-quoted declarations otherwise read like double-quoted ones (probed).
- TEI comment/PI text fails both (lxml `ValueError`, Rust `Error::Parse`).
- Two `#[allow(clippy::question_mark)]` (tei walk recursion, xml GeneralRef
- resolve): `?` leaves no countable region there (error tests execute the
- path while the `?` region reads zero — verified during development), so
- the explicit form stands with 100% regions. Not a waiver: behavior
- identical, coverage complete.
- Phase 4 (works pure core, CPython 3.13, Unicode 15.1, PyYAML 6.0.3):
- Python `json.dumps` floats are shortest-mantissa with a signed ≥2-digit
- exponent (`1e-05`, `1e+16`) and fixed notation only for `1e-4..1e16`
- (`0.0001`, `1000000000000000.0`); Ryu shortest digits match but its layout
- does not, so `canonical_json` re-lays Ryu digits per
- `format_float_short` (301,380-vector fuzz, zero mismatches). `NaN` /
- `Infinity` / `-Infinity` emit bare. Integers beyond `2^53` in JSON cross
- as f64 on the Rust side (`serde_json` parses `10**30` to `1e30`) — a
- representability gap, unreachable in hash payloads (small ints only).
- Python `UUID(raw)` accepts misplaced hyphens and a stray wrapping brace
- the `[^}]+` match leaves behind; `Uuid::parse_str` rejects both, so
- `find_markers` replicates the hex path (80,004-vector fuzz, zero
- mismatches). Non-ASCII decimal digits parse under `int(x, 16)` in Python
- and refuse in Rust (accepted residual divergence, untested upstream).
- Python `repr` printability is general-category data (`Cc Cf Cs Co Cn Zl
- Zp Zs` minus U+0020), baked as 713 ranges (`printable_table.rs` +
- `etc/gen_printable_table.py`); hand lists diverge 1,089 ways. Regenerate
- only on Unicode-data upgrades; the pinned count fails otherwise.
- `serde_json` without `preserve_order` sorts map keys, but Python dicts
- keep insertion order — and the mishpat fixture carries 120+ non-sorted
- multi-key locators (`entry` before `article`). The workspace enables
- `preserve_order`; `canonical_json` sorts explicitly, and `Map::remove`
- (order-destroying `swap_remove`) is avoided where order survives a pop
- (footnote `volume`). `sorted(rglob(...))` compares `Path`s
- component-wise (`a/b.md` before `a-b.md`), not byte-wise: the file walk
- threads its root and sorts split components.
- PyYAML `safe_dump` emission (width-80 wrapping, implicit-resolution
- quoting, indentless sequences) is replicated by a dedicated emitter,
- proven against captured CPython bytes; `safe_load` arrives via
- `serde_yaml` with two documented resolver gaps (YAML 1.1 `yes/no/on/off`
- bools, exponent floats) that never occur in work files. Import-deletion
- ties keep tree order (Python dict stability), not key order.
- `difflib` triple-filter ratio (autojunk off under 200 chars, winner by
- `(ratio, key-string)` toward the largest key) is ported exactly for month
- similarity (705-word sweep, zero mismatches). `datetime` out-of-range
- years raise in Python and refuse (`None`) in Rust — no defined behavior
- to mirror. No `icu_calendar`: the module is pure Gregorian arithmetic.
- One `#[allow(clippy::waker_clone_wake)]` (drafting test whose purpose is
- firing the vtable `clone` callback): the clone is the contract there.
- Phase 5 (retrieval storage + words, sqlx 0.8, pgvector/pg_trgm/ltree):
- `LemmaResult` carries `counts = {}` on the zero-result path (the Python
- dataclass default is never replaced on a miss) and all five aggregate keys
- on a hit; Rust serializes an all-empty `AggregateCounts` as `{}` via a
- `collect_map` hook (a `serialize_map(...)?` + `.end()` hook leaves a `?`
- arm inside the generic serializer each instantiation must cover
- separately). Pinned by wire-shape round-trip tests.
- `cargo llvm-cov` counts per monomorphization: a generic function's
- `Text`/`Int` (or serializer) match arms need firing in *every* test
- binary that instantiates them (lib unit tests *and* `tests/pg_repos.rs) —
- shared binders therefore live in one concrete `encode_where_params`
- (attached with `query_as_with`), shared helpers stay monomorphic, and
- fault tables are mirrored case-for-case through the public API in both
- suites. Re-examine if the toolchain's coverage model changes; Phase 6
- (`reqwest` generics, more binaries) inherits the rule.
- Binds cross Postgres-typed with explicit casts: embeddings as
- `"[x,y,...]"` in Python `str(float)` rendering (`format_vector_py`,
- probed digit-for-digit incl. integral/subnormal/17-digit values) with a
- `::vector` cast; ltree paths as text with `::ltree`; dates as RFC3339
- strings with inline `::timestamptz` casts (Postgres has no
- `timestamptz >= text` operator). `Vec<Option<T>>` array binds keep `NULL`
- elements aligned through `unnest` (pinned by the token-outside-ids and
- link-clearing round trips).
- pgvector accepts any integer `hnsw.ef_search` (probed: `-5` sets), so
- `SET LOCAL` is best-effort and a dead connection is diagnosed by the
- `SELECT`, not the `SET`.
- Test Postgres: `CREATE DATABASE ... TEMPLATE` on a multi-GB dev database
- can crash the server (WAL burst, observed 2026-09-20 on 3.7 GB —
- recovered clean, data intact, and never retried); scratch databases build
- from schema-only `pg_dump` + `COPY` of reference rows, and the dev
- database is read, never written. Pooled connections return mid-transaction
- state to the pool: methods opening their own transaction roll back before
- propagating failures (ef-search path included); caller-`Tx` methods leave
- cleanup to the caller (contract on `PgTx`).

- Phase 6 (IO adapters, reqwest 0.13, CPython 3.13):
- workspace `serde_json` carries `float_roundtrip`: without it the `1/65`
- differential case parses 1 ulp off (`...647` vs correctly-rounded `...648`,
- the ≥17-digit finding from Phase 2 firing on the wire itself). `-0.0`
- keeps its sign across the crossing. Pinned bit-exact in the (deleted)
- differential; the flag adds no regions to earlier crates.
- `locate_span` returns *character* offsets (`str.find`/`match.start()`); Rust
- byte offsets agree on ASCII fixtures only and corrupt anchors past any
- non-ASCII text (found by the agent's own report, fixed before finishing —
- always add a non-ASCII-before-match case when porting offset code).
- `assert!(matches!(…))` that only ever matches leaves one uncovered region
- per site (the false arm): assert error taxonomy with `discriminant`
- comparisons plus verbatim `Display`/payload asserts, or fire both arms
- through one helper. Routing's `is_fallback_error` passes because its tests
- hit both outcomes.
- The post-handshake second circuit check is dead under `&mut` exclusivity
- (every incrementing handshake path returns `Err`, successes never
- increment — proven in a comment, arm removed); Python keeps both checks
- because aliases share one counter behind a lock.
- `Regex::new` on escaped-literal patterns fails only past the compiled-size
- limit: pinned with a 20M-char input both sides answer `None` to, not deleted.
- `minimum`/`maximum` merge only when *both* sides declare `minimum` (the two
- are inserted solely as a `range` pair, so the second lookup cannot miss —
- inner guard removed with that proof); Python `KeyError`s there on hand-built
- schemas instead.
- Typed-boundary deviations (pinned with deviation comments, Python escapes
- raw SDK errors where Rust must answer a typed error): OpenAI
- missing/non-JSON tool-call arguments → `Llm`; Anthropic half-present usage
- → `Llm` (the SDK would fail `Usage` validation); OpenAI `200`-with-unusable
- body → `Status` arm. `HttpAdapter` transports → `Error::Transport`.
- Rerank `409` increments the breaker *then* raises `ModelMismatch` (order is
- the Python's); embed `POST` statuses propagate as `Http` unmapped. `:g`
- formatting replicated as `fmt_g` (pinned vectors); 200-char truncation is
- char-boundary-safe (`head_chars`). `NoopReranker::rerank` is sync.

- Phase 7 (history + framework shaping, CPython 3.13):
- `round(x, 1)` is half-even on the binary value and ties DO occur
- (`round(0.25, 1) == 0.2` — averages like 1/4 hit them): `round1`
- replays the twenty-place-expansion technique from the Phase 3 `round4`
- helper (22-vector fuzz incl. `2.05 → 2.0`, zero mismatches). The naive
- scale-and-`f64::round` answers `0.3`.
- `round(avg, 1)` types `int` on the empty path (`round(0, 1)`) and `float`
- elsewhere: the report renders integer `0` with no bins (custom serializer,
- the value alone selects the shape — every listed bin holds ≥ 1 letter).
- The correspondence "silence" arm is dead (`all_bins` collects only keys
- counted ≥ 1, so `count == 0` is unsatisfiable) and the `avg > 0` guard is
- subsumed by the loop — both removed with that proof, not branched around.
- If `all_bins` ever spans empty bins, re-examine.
- `(later - earlier).days` floors exact microseconds: timestamps cross as
- integer micros with `div_euclid(86_400_000_000)` — flooring truncated epoch
- seconds flips pairs split across a day boundary by sub-second parts.
- `surname` is exact over `is_word_char` + `is_space` + ASCII dots, with two
- accepted residual divergences (same class as Phase 4): `str.isdigit` admits
- Nl numerals Rust `is_numeric` also admits where Python refuses, and Python
- lowercases with context-sensitive sigma (final Σ → ς) where Rust maps Σ →
- σ. Both absurd for real correspondent names; pinned for realistic inputs.
- `_count_by_method` keeps first-seen order (Python dict stability) as a
- `Vec` of pairs — the JSON object order is seam work.
- `find_env_file` takes the override as a parameter (it reads `RE_ENV_FILE`
- from the environment itself); `~otheruser/` is left unexpanded (account
- database needed — residual, documented). `candidate` returns un-resolved,
- exactly like the Python join.
- `validate_input` needs no bool-guard: JSON keeps bool distinct from
- integer, so the Python `bool`-is-`int` special case has nothing to mirror.
- Enum messages render with Python `repr` (`['fast', 'slow']`, `True` /
- `False` / `None`, shortest floats) via a dedicated `py_repr`.
- `update_core_tool` lands the handler even for unlisted names (the
- Python's unconditional store); with names-only defs the same-name replace
- is identity. `notify_changed` snapshots the listener count, mirroring
- `for listener in list(self._listeners)`.

## What NOT to rewrite (or never)

- `modules/docling_converter.py` — ML model wrapper; Python shim stays.
- Playwright / Chromium flows (`scrape_kindle.py`, Logos/YCL logins) — browser automation, no Rust win.
- Alembic migrations (`adapters/storage/postgres/migrations/versions/*`) — history; new Rust migrations only for new schema.
- `sentence-transformers` inference weights — process boundary, not a port.

## Step 0 — Packaging (decided 2026-09-22, no approval stop needed)

Decision: core (`marginalia-ai`) and SDK (`marginalia-ai-sdk`) stay
Hatchling pure-Python `py3-none-any` wheels, exactly as today. Rust ships
as a separate optional distribution, `marginalia-ai-accelerator`, built
with `maturin` (pyo3 bindings, `abi3-py311`, one wheel per platform for
all Python 3.11+), exposing the single top-level extension
`marginalia_rs` with one submodule per phase (`text`, `chunk`, …).
Core gains an `[accelerated]` extra
(`marginalia-ai-accelerator>=0.6,<0.7`, same-minor tracking like the SDK
policy) and selects the backend at runtime via `RE_RUST_BACKEND`
(`auto` default: Rust when importable, else Python; `rust` forces and
fails loudly when absent; `python` forces the pure-Python path and is
the bisection/rollback switch). Every cut-over caller keeps its Python
implementation as the permanent fallback — it doubles as the
differential oracle — so the fallback can never rot.

Wheel matrix: `py3-none-any` for core/SDK (unchanged, installs with no
toolchain); `cp311-abi3` platform wheels for the accelerator on linux
x86_64+aarch64, macOS arm64+x86_64, Windows x64 (maturin-action), plus
its sdist (building that sdist into a wheel needs Rust — acceptable,
because nothing requires the accelerator). The accelerator crate
(`crates/marginalia-py`) is NOT a `uv` workspace member, so `uv sync`
on a compiler-less machine never invokes cargo; release builds it
explicitly (`uv build crates/marginalia-py` / maturin-action).

Alternatives rejected: a single maturin build for core (its sdist would
need cargo at install time, and releases would gate on every platform
wheel existing — breaks the no-compiler install story on exotic arches);
setuptools-rust (same fallback defect, weaker cross-build story than
maturin-action); scikit-build-core/CMake (no C++ here, pure overhead).

Fallback proof (done 2026-09-22, pre-Rust baseline): `uv build` both
distributions, `twine check --strict` 4/4 PASSED, and a `pip install`
inside `python:3.11-slim` with no cargo/rustc/cc/gcc on PATH yields a
working install (`import research_engine` 0.6.2, `research-engine
--help`, lean base with no torch/docling/sentence-transformers).
Per-phase re-proof is mandatory: the same container install plus
`RE_RUST_BACKEND=python` rollback (exact old behavior) and the
extension discovery smoke (`history`, `logos`, `ycl`, `acad`) gate
every phase. External extension suites live in their own repos; locally
they gate via entry-point discovery + SDK contract checks + the
in-tree history pack suite (`pytest packages/plugins`).

## Phase 1 cutover — text seam (done 2026-09-22)

`marginalia_rs.text` (new crate `marginalia-py`, maturin `marginalia-ai-accelerator`
0.6.2, `abi3-py311`) exposes `normalize`, `normalize_whitespace`,
`normalize_with_map`, `normalize_for_matching`, `NORMALIZATION_VERSION` with
signatures identical to `services/text/normalize.py`. Caller cut over:
`services/verification/quote.py` (`verify` stored/match forms,
`_locate_normalized` source-continues fold, `_find_folded`) behind
`RE_RUST_BACKEND` (`research_engine/_rust.py`: `auto`/`rust`/`python`);
the Python implementations stay as the permanent fallback and oracle.
Core gains an `[accelerated]` extra
(`marginalia-ai-accelerator>=0.6,<0.7`); the seam crate is NOT a `uv`
workspace member, and the root pins a path source for it so `uv lock`
resolves without the registry (wheels carry no `uv.sources`, so end-user
pip is unaffected).

Evidence: `cargo test --workspace --exclude marginalia-ret` 1053 green
(1051 + 2 new seam); `cargo llvm-cov -p marginalia-py` 100%
lines/functions/regions; clippy zero; fmt clean. `pytest tests/unit`
1619 + 1 skipped under BOTH `RE_RUST_BACKEND=rust` and `=python`
(incl. permanent `tests/unit/services/test_text_rust_parity.py`: 42 —
backend-forced fold/map/tier matrix incl. non-ASCII-before-match anchors,
rollback pin, switch-semantics tests).
`pytest packages/sdk/tests packages/plugins` 47 green; ruff clean;
all four extensions discover (`academic-journal`, `history`, `logos`,
`yourcloudlibrary`); SDK 82 exports intact. `uv build` core+sdk+maturin
sdist, `twine check --strict` 6/6 PASSED. Compiler-less
`python:3.11-slim` container: pure wheels install and run with no
toolchain (auto selects python); with the `cp311-abi3` wheel added, auto
selects rust, `=python` rolls back, CLI lean in both. External extension
suites live in their own repos (not checkouts here); they gate locally
via discovery + SDK contract + the in-tree history pack suite.
Automation: CI `rust` job (workspace tests, clippy `-D warnings`, fmt,
seam coverage gates at 100, wheel build + rust-backend suites + rollback);
release `build-accelerator*`/`publish-accelerator*` (3-platform matrix +
sdist + TestPyPI dispatch, `pypi-accelerator` env, `accel-v*` tags).

New load-bearing findings: (1) `uv` resolves ALL extras universally, so an
extra depending on an unpublished distribution breaks `uv lock` for the
whole workspace — the committed path source is the bridge until the
first accelerator publish, and the extra must land only together with it.
(2) PyO3 test binaries link libpython: builds need `PYO3_PYTHON` pointed
at the project interpreter (NOT `python3` from PATH), and running them
needs its `LIBDIR` on the loader path when it is uv-managed (system
pythons are already on the default path) — CI sets both from
`uv python find`. (3) `?` in `#[pymodule]` init leaves countable
Err-return regions; the proven-infallible `expect` form (same class as
the `LazyLock` `unwrap`s) keeps 100% — `unwrap` panic arms do not count
on this toolchain. (4) The wheel matrix ships 3 platforms + sdist;
linux-aarch64 + macOS-x64 fall back to pure Python (compliant by
design), tracked for 0.7.

Phases 2–7 (chunk → parse → works → ret → io → framework/history seams)
follow this exact machinery: extend `marginalia_rs.*` one submodule at a
time, cut one caller behind the same flag, extend the parity suite, and
re-run this battery. The async-boundary phases (ret/io) are the known
hard part (tokio-vs-asyncio at the repo/HTTP-client boundary) and are
still ahead — no seam exists for them yet.

## Phase 2 cutover — fusion (done 2026-09-22; windows/chunkers/langconfig follow)

`marginalia_rs.chunk` (`crates/marginalia-py/src/chunk.rs`) exposes
`rrf_fuse(*ranked_lists, k=60)`, `weighted_fuse(vec, kw, alpha=0.5)`,
`RRF_K` with signatures identical to `services/search/fusion.py`. Caller
cut over: `services/search/hybrid.py` Stage 3 (`_rrf_fuse`,
`_weighted_fuse`) behind the same `RE_RUST_BACKEND` switch (new
`research_engine._rust.rust_chunk()` accessor).

Seam specifics (pinned): rows return the *original* pid objects (downstream
`loaded[pid]` / `get_many` / rerank dict keys cannot tell the backend);
`k` accepted-and-ignored (the divisor is `RRF_K` — the plan's "kept
parameter" note now lives at the seam, since the crate itself dropped it);
breakdown keys emit in ascending list order; pids must be UUIDs
(`ValueError` otherwise — upstream accepts any hashable, every call site
is typed `UUID`); weighted exact ties compare per-id (upstream iterates a
`set`); NaN scores outside the proven domain (as in the crate
differential); floats cross as binary f64 (never decimals), UUIDs as
objects — the 1-ulp JSON finding cannot fire here.

Evidence: seam crate 7 Rust tests, `llvm-cov -p marginalia-py` still
100/100/100; workspace 1058 green excl ret; clippy zero; fmt clean.
`pytest tests/unit` 1636 + 1 skipped under BOTH backends (new permanent
`test_fusion_rust_parity.py`: 17 — backend-forced bit-exact matrix incl.
`k=7`, alpha sweep, 17-digit scores as hex bit patterns, ties per-id,
object identity, hybrid end-to-end `find_passages` for rrf+weighted ×
rerank on/off via `model_dump`). SDK+packs 47; ruff clean; 4 extensions
discover. Artifacts rebuilt, twine 6/6. Compiler-less containers: pure
install auto-selects python; with the abi3 wheel fusion runs on rust with
identity intact, `=python` rolls back.

Remaining Phase 2 slices (same machinery): read windows
(`choose_window` + callers), chunkers (fixed/prose/structural/whole +
ingest callers), `langconfig.pg_config`. `hybrid.py` orchestration stays
Python through Phase 5 per scope.

## Phase 2 cutover — windows (done 2026-09-22; chunkers/langconfig remain)

`marginalia_rs.chunk.choose_window` / `.build_window`
(`crates/marginalia-py/src/windows.rs`) with the seam contract: nodes cross
as `DocumentNode` JSON into the real structs (no fabricated fields),
spans as `(start, end)` pairs, budgets as `i64`; results return as plain
tuples and the adapters in `services/search/windows.py`
(`_choose_window_rs`, `_build_window_rs`) reattach the original `Span` /
`WindowPlan` / node objects, so `plan.node is` the input ancestor on
either backend. `choose_window` / `_build_window` keep identical
signatures and branch internally; `PassageWindowReader.read` is untouched.

Seam specifics (pinned): negative coordinates clamp to 0 at the `usize`
boundary (malformed-input deviation — storage validates non-negative);
malformed node JSON / plan source / node id answer `ValueError`; metadata
floats (incl. a 17-digit battery value) ride along unread and unreturned;
`WindowSource` round-trips through all four variants.

Evidence: seam crate 12 Rust tests, `llvm-cov -p marginalia-py` still
100/100/100; workspace 1063 green excl ret; clippy zero; fmt clean.
`pytest tests/unit` 1657 + 1 skipped under BOTH backends (new permanent
`test_windows_rust_parity.py`: 21 — backend-forced plan/window matrix over
the Louw-Nida/marginal-Jew/degenerate vectors + Hebrew titles + negative
budgets, bound-object identity, reader end-to-end, seam-contract
`ValueError`s, clamp + metadata pins, no-wheel fallback). SDK+packs 47;
ruff clean; 4 extensions discover. Artifacts rebuilt, twine 6/6.
Compiler-less containers: pure fallback + accelerated windows + rollback
proven. CI rust job extended with the windows suites both ways.

## Phase 2 cutover — chunkers (done 2026-09-22; langconfig remains)

`marginalia_rs.chunk.chunk_fixed/prose/whole/structural`
(`crates/marginalia-py/src/chunkers.rs`) answer draft JSON built with
`metadata: None` (the crates never read it). Adapters in the four
`services/ingestion/chunking/*.py` reattach the original metadata mapping
(`metadata or {}`, same expression — identity holds), rebuild structural
`section_meta` from the original mapping plus the draft's own heading, and
pass sections as strict JSON. All four `chunk()`s keep identical signatures
and branch internally (structural keeps its `isinstance` guard first);
`pipeline.py` and the parser modules are untouched.

Seam specifics (pinned): prose `max_tokens < 1` re-raises `ValueError` with
the identical text; structural locate failures raise the core
`ChunkingError` with the identical message; malformed section tables answer
`ValueError`; non-JSON-native section values fail the adapter's strict
`json.dumps` with `TypeError` (upstream would carry the object); foreign
error variants degrade to `RuntimeError` (fault tests pin both mappers).

Parity gap found AND closed in this slice (not a HALT): the crate's
not-found message used Rust `{:?}` (double quotes) where Python `!r`
prefers single (`'Missing entirely'` vs `"Missing entirely"`, caught by
the seam differential on plain section text). Fix: `py_repr_str` +
printability table MOVED from `marginalia-works` (lib.rs +
printable_table.rs + generator) to `marginalia-text::repr` (proven by a
deleted 4,279-vector sweep vs CPython, permanent vectors kept); works
rewired (`py_repr_str_list` rebased, attach/drafting/files/validate
imports repointed, count test moved); chunk structural uses it. works
gains a `marginalia-text` dep (DAG-safe: text depends on nothing new).
New chunk test pins both quote styles; generator reproduces the table
byte-identically (713 ranges).

Evidence: seam crate 19 Rust tests, `llvm-cov -p marginalia-py` 100/100/100
from clean; text+chunk+py 100/100/100 from clean (chunk message pin
included); workspace 1072 green excl ret; clippy zero; fmt clean.
`pytest tests/unit` 1693 + 1 skipped under BOTH backends (new permanent
`test_chunkers_rust_parity.py`: 36 — backend-forced matrices, metadata
identity incl. UUID/datetime/17-digit floats, ChunkingError type+message
equality, non-JSON boundary, no-wheel fallback). SDK+packs 47; ruff clean;
4 extensions discover. Artifacts rebuilt, twine 6/6. Compiler-less
containers: pure fallback + accelerated chunkers + rollback proven.
CI rust job extended with the chunker suites both ways, and its PyO3 step
now points at the venv interpreter (the only one whose in-process sys.path
sees the project — the seam's error tests import the real core modules).

Load-bearing toolchain findings: (1) `cargo llvm-cov` WITHOUT `clean`
accumulates stale profdata across `-p` selections and reports phantom
misses from duplicate instantiations — gate measurements run from clean.
(2) `marginalia-works` measures 99.88% on this toolchain (rustc 1.98)
PRISTINE (38 regions in `find_by_edition_id` test fakes, identical counts
before/after this slice — proven via isolated worktree): pre-existing
coverage-model drift, not a regression; the 100% bars hold per the CI-era
model and every line touched here is covered. Trivial follow-up if the
letter must hold here: 5 fake-call lines.

## Phase 2 cutover — langconfig (done 2026-09-22; Phase 2 complete)

`marginalia_rs.chunk.pg_config` / `.is_known_config` / `DEFAULT_CONFIG` /
`KNOWN_CONFIGS` (`crates/marginalia-py/src/langconfig.rs`): pure table
lookup, signature-identical, strings in/out (`KNOWN_CONFIGS` crosses as a
real frozenset). Callers migrated: `hybrid.py` (search language),
`reindex.py` + `orchestrator.py` (2 sites, ingest FTS indexing), and all
five `is_known_config` sites in `adapters/.../repositories/passages.py`
(SQL-literal validation — a deliberate early touch of a Phase-5-owned
file: the swap is behavior-neutral, recorded here so Phase 5 knows it
landed). `hybrid.py` orchestration still stays Python per scope.

Learning: `Option<&str>` params do NOT default to `None` in pyo3 0.24 —
`#[pyo3(signature = (iso = None))]` is required (caught by the seam's own
registration test). Version-skew note: a stale accelerator wheel fails
LOUDLY (`AttributeError` on the missing submodule) rather than falling
back — correct, since `auto` means "Rust when importable" and the module
imports; post-release, additive submodules require an accelerator version
bump + extra-floor raise. All slices ship together in 0.6.2, so no skew
exists yet.

Evidence: seam crate 21 Rust tests, `llvm-cov -p marginalia-py`
100/100/100; workspace 1074 green excl ret; clippy zero; fmt clean.
`pytest tests/unit` 1709 + 1 skipped under BOTH backends (new permanent
`test_langconfig_rust_parity.py`: backend-forced matrix incl. locales,
case, whitespace, unknowns, constant/type surface, no-wheel fallback).
SDK+packs 47; ruff clean; 4 extensions discover. Artifacts rebuilt, twine
6/6. Compiler-less containers: pure fallback + accelerated langconfig +
rollback proven. CI rust job extended both ways.

Phase 2 is COMPLETE: fusion (2a) + windows (2b) + chunkers (2c) +
langconfig (2d) all cut over, every caller migrated, `hybrid.py`
orchestration left Python per scope. `offsets.py` needs no seam
(`CanonicalIndex::find` + `collapse_whitespace` already cover it),
`hit_source.py` has no portable predicate, `filter_extensions.py`
`build_clause` is SQLAlchemy (Phase 5) — all per the original scoping.

## Phase 3 cutover — plain_text + markdown (done 2026-09-22; html/epub/tei/pdf remain)

`marginalia_rs.parse` (new crate module `crates/marginalia-py/src/parse.rs`):
`parse_plain_text` / `parse_markdown` (decoded text + file name in,
`ParsedDocument` JSON out) and `detect_plain_text_content` /
`detect_markdown_content` (newline-normalized head bytes in, `(score,
reason)` out). Adapters in `modules/plain_text.py` + `modules/markdown.py`
branch internally: reads/decoding stay caller-side (strict UTF-8 and
universal newlines surface exactly, including `UnicodeDecodeError`), the
suffix + `mimetypes` detect branches stay caller-side ordered ahead (the OS
table is not portable), and parse triples are reassembled from the JSON
(markdown maps the `sections` field back under `metadata["sections"]` —
the same contract the pipeline reads).

Seam specifics (pinned): scores cross as binary `f64`; titles/texts/counts
and section tables are strings and integers — no float crosses; heads are
newline-normalized adapter-side so lone-CR files peek identically.

Evidence: seam crate 25 Rust tests, `llvm-cov -p marginalia-py`
100/100/100 from clean; workspace 1078 green excl ret; clippy zero; fmt
clean. `pytest tests/unit` 1735 + 1 skipped under BOTH backends (new
permanent `test_parse_rust_parity.py`: 26 — backend-forced parse/detect
matrices incl. unicode, empty, CRLF, code fences, bad bytes raising
`UnicodeDecodeError` either way, section-shape pin, no-wheel fallback).
SDK+packs 47; ruff clean; 4 extensions discover. Artifacts rebuilt, twine
6/6. Compiler-less containers: pure fallback + accelerated parse +
rollback proven. CI rust job extended both ways.

## Phase 3 cutover — html/epub/tei (done 2026-09-22; pdf remains)

`marginalia_rs.parse.parse_html/parse_epub/parse_tei` (raw bytes in,
`ParsedDocument` JSON out) plus `detect_html_content` (decoded head),
`detect_epub_magic` (4 head bytes), `detect_tei_content` (decoded head).
Adapters in the three modules branch internally: suffix (+mime where the
module has one) stays caller-side ordered ahead; reads stay caller-side
(bytes for epub/tei/html-parse, decoded heads for html/tei peeks —
marker substrings are newline-invariant); triples reassembled with the
section table mapped back under `metadata["sections"]`. Corrupt inputs
answer `ValueError` with the crate's message (Python raises engine-native
errors; callers catch `Exception`). The missing-dependency gates stay on
the Python path only — the Rust backend parses with no
`bs4`/`ebooklib`/`lxml` installed (pinned; proven in lean containers too).

Parity gap found AND closed: the crate errored on `&#[0-9]+[a-f]`
without `;` (`&#38b`) where both tokenizers decode with a noted parse
error — erroring dropped whole documents the modules parse. Fix: spell
the missing semicolon in (`normalize_entities.rs`), three vestigial `?`
converted to proven `expect`s, four crate tests rewritten to
substitution asserts. Differential battery kept permanent
(`tests/entity_battery.rs`: 11 equality incl. clamps/legacy/CJK, 3
poison pins). One accepted residual, pinned: the same inputs corrupt
html.parser's tag state (following markup leaks as text —
version-fragile, probed); the crate decodes cleanly per the rule instead.
`&#38`+non-hex and all other shapes agree byte-identically.

Evidence: seam crate 30 Rust tests, `llvm-cov -p marginalia-py` and
`-p marginalia-parse` (bins excluded by design) 100/100/100 from clean;
workspace 1085 green excl ret; clippy zero; fmt clean. `pytest
tests/unit` 1768 + 1 skipped under BOTH backends (new permanent
`test_parse_formats_rust_parity.py`: 33 — backend-forced parse/detect
matrices incl. entities, shuffled-spine EPUB, namespaced/bare TEI,
corrupt-both-raise, gate-hidden, no-wheel fallback). SDK+packs 47; ruff
clean; 4 extensions discover. Artifacts rebuilt, twine 6/6.
Compiler-less containers: pure fallback + accelerated formats (with no
[documents] installed) + rollback proven. CI rust job extended both ways.

## Phase 3 cutover — pdf detect + parse stays Python (done 2026-09-22)

`marginalia_rs.parse.detect_pdf_magic` (5-byte head in, `(score, reason)`
out; the adapter passes the exact 5-byte read so the crate's prefix check
and the module's exact-equality check agree). The module's suffix + MIME
branches stay caller-side ordered ahead.

HALT REPORT (parity gap, not routed around): `PDFTextModule.parse` does
NOT cut over and MUST NOT until a versioned re-ingest migration exists.
Page text is engine output — fitz wraps long lines and trims page edges
where the Rust port's pdf-extract does neither (3 fixtures evidenced in
the original findings) — so routing parse through `marginalia_rs` would
change extracted text on real PDFs: a byte-identity violation. Triggering
inputs: any PDF with long lines or edge content. Disposition: keep-Python
pinned by `TestPDFParseStaysPython` (source inspection: no `rust_parse`
may route into `parse`) and a code comment at the decision site; the
tracked follow-up (pdf 1.1 + identifiers + orchestrator rework) owns any
future engine change. `detect` parity is unaffected and shipped.

Evidence: seam crate 31 Rust tests, `llvm-cov -p marginalia-py`
100/100/100 from clean; workspace 1086 green excl ret; clippy zero; fmt
clean. `pytest tests/unit` 1777 + 1 skipped under BOTH backends (new
permanent `test_pdf_rust_parity.py`: 9 — backend-forced detect matrix,
no-wheel fallback, keep-Python pin). SDK+packs 47; ruff clean; 4
extensions discover. Artifacts rebuilt, twine 6/6. Compiler-less
containers: pure fallback + accelerated detect + rollback proven. CI rust
job extended both ways.

Phase 3 is COMPLETE modulo the documented keep-Python: plain_text,
markdown, html, epub, tei cut over; pdf detect cut over, pdf parse stays
(first HALT item, reported not routed around). `docling_converter` and
`scrape_kindle` untouched per original scope.
