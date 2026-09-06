# Works and citations — implementation guide

**Status:** Implementation guide, written 2026-09-04 against the engine at
`/home/john/repos/MarginaliaAI` @ `3b5251d` (migrations 001–008 built). It
implements `works-architecture-master.md` as amended on 2026-09-04, whose
fourteen decisions are recorded with their options in
`works-architecture-diagrams.md`. Phase 0 (Steps 1–2) is ready to build.
Steps 3–7 are specified to the level the docs allow and are gated on Phase 0
having run; do not start them first.
**Audience:** an implementer, human or LLM, who has not read the design docs.
**Companions:** the master (controlling), the diagrams doc (pictures and
decisions), `works/README.md` and `works/_TEMPLATE.md` (the file contract).

## Contents

- Part 0 — Read this first: how to use the guide, invariants, environment, code map, test isolation
- Step 1 — Substrate: tests, the first real work, the committed contract
- Step 2 — Phase 0: the file tools and the citation draft
- Step 3 — Migrations 009 and 010: the span table and the ledger
- Step 4 — Migration 011: the bridge mirror (conditional)
- Step 5 — The flip rehearsal (F6)
- Step 6 — Migration 012 and the Phase 1 spine
- Step 7 — On their triggers, not in this guide
- Appendix A — Rule id catalogue
- Appendix B — Tool contracts
- Appendix C — Fixtures
- Appendix D — Intent, role, and granularity
- Appendix E — Fixed by this guide
- Appendix F — Change log

---

## Part 0 — Read this first

### 0.1 How to use this guide

This guide is written to be handed to an implementer, human or LLM, who has
not read the design docs. It is organised as one chapter per step of the build
order. Every step has a **gate** (what must be true before you start), a
**scope**, explicit **non-goals**, a **test table**, and a **done-when**. Do not
start a step whose gate is not met, and do not report a step done while any
row of its test table fails. Where you must deviate, write the deviation and
its reason in the change log at the end of this guide before moving on.

Precedence when documents disagree: the master doc as amended on 2026-09-04,
then the decisions table in the diagrams doc, then this guide's items marked
**fixed by this guide** (listed in Appendix E for the researcher to review),
then the companion docs. Nothing in a companion doc overrides the master.

### 0.2 Invariants — never violate these

1. **Anchor to `(document_id, char_start, char_end)` into
   `core.document_texts.text`. Never to `passage_id`.** Passages are deleted
   and reinserted on re-chunk. `passage_id` is a `SET NULL` cache and nothing
   more.
2. **Never report `normalized` as `exact`.** The five tiers are `exact`,
   `normalized`, `near`, `not_found`, `no_canonical_text`. Only the first three
   are ever stored, and `no_canonical_text` is a different answer from
   `not_found`.
3. **Search never writes.** `find_passages` returns a citation draft; a
   citation exists only after a verify-then-write through one path.
4. **One span row per `(document_id, char_start, char_end)`.** Every writer
   goes through the resolver. No writer inserts a span directly.
5. **The span owns the address and the canonical slice. The citing row owns
   the typed quote, the tier, the timestamp, and the locator.** Decision 1.
6. **Every cited source is `ON DELETE RESTRICT`.** You may not delete a
   document a work or a claim rests on.
7. **Rendered citation strings are never authoritative.** Free text enters
   nowhere; it is emitted only by renderers.
8. **Works are not `core.documents`.** Authored text never becomes corpus
   evidence and never enters `core.passages`.
9. **No tables outside Alembic.** Every table is a numbered migration in the
   core package, declared in `schema.py`, additive, and independently
   revertible.
10. **Agents propose; a human act commits.** In Phase 0 the commit is accepting
    a file edit. In Phase 1 the commit is the freeze. Rows an agent writes
    before a freeze are drafts.
11. **`stdout` is the MCP transport.** Never `print()` from a tool or service;
    use `structlog`.
12. **Tests remove exactly the rows they create.** The integration suite runs
    against the real dev corpus and asserts it is unchanged afterwards.

### 0.3 Environment

| Item | Value |
|---|---|
| Engine repo | `/home/john/repos/MarginaliaAI` (branch per topic, `John-Cusack/<slug>`, merged by PR) |
| Package root | `/home/john/repos/MarginaliaAI/packages/core/src/research_engine` (all paths below are relative to this unless stated) |
| Python / tooling | Python 3.11 target; everything through `uv run`; lint is ruff only (`ruff.toml`: line length 100, rules `E F I UP B SIM TCH`, no mypy) |
| Dev database | `make db` starts `tools/dev-postgres/docker-compose.yml`: image `pgvector/pgvector:pg15`, host port **5435**, db `research_engine`, user `re_dev`; `init.sql` creates schema `core` and extensions `vector`, `pg_trgm`, `uuid-ossp`; tables come from Alembic |
| Migrations | `make migrate` = `uv run alembic -c packages/core/src/research_engine/adapters/storage/postgres/migrations/alembic.ini upgrade head` |
| Settings | `config/settings.py`, class `Settings(BaseSettings)`, env prefix `RE_`; `load_settings()` finds the nearest `.env` upward from the cwd (or `RE_ENV_FILE`); `RE_DB_URL` defaults to the dev database |
| Tests | repo-root `tests/unit` and `tests/integration`; `pytest.ini` sets `asyncio_mode = auto` and markers `unit`, `integration`, `contract`, `slow`; `make test` (unit), `make test-integration` (needs `make db`, skips without it), `make lint` |
| CI | `uv sync --group dev`, ruff, `pytest tests/unit -q`, `pytest packages/plugins -q`; integration tests are not run in CI |
| Docs to keep in sync | `CHANGELOG.md` (prose, newest first, one `###` per change); commit subjects are imperative sentences with a body ending in a "Verified by …" line |

### 0.4 Code map — where things live and what to copy

| Kind of change | Where | Copy this |
|---|---|---|
| MCP tool | `mcp/tools/<name>.py`, then import and append to `CORE_TOOL_MODULES` in `mcp/dispatch.py` | `mcp/tools/verify_quote.py` (module attributes `TOOL_NAME`, `TOOL_DESCRIPTION`, `TOOL_SCHEMA`, `async def handler(container, *, …)`) |
| Tool input validation | `mcp/dispatch.py:_validate_input` checks `required`, primitive `type`, `enum` only; nested objects and `format` are documentation, validate them in the handler | — |
| Tool errors | raise, and dispatch returns `{"error": {"code": "<tool>_failed", "message", "details": None}}`; or return that envelope yourself with `invalid_input`, `not_found`, `validation_error`, `conflict` | `mcp/tools/find_passages.py` error path |
| Service | `services/<area>/<name>.py`; constructor takes repos and, for writers, `transaction_factory` | `services/extraction/executor.py` (takes `transaction_factory`), `services/verification/quote.py` |
| Repository | `adapters/storage/postgres/repositories/<name>.py`, class `PG<Thing>Repo(engine: AsyncEngine)`; writes take `tx: Transaction` first and never commit; reads open their own connection; Protocol in `ports/repositories.py`; re-export in `repositories/__init__.py` | `repositories/entities.py`, `repositories/document_texts.py` |
| Transaction | `adapters/storage/postgres/engine.py:transaction(engine)` async context manager yielding `Transaction(conn)`; multi-table example in `services/ingestion/orchestrator.py:205-255`; dry-run by `await tx.conn.rollback()` as in `services/ingestion/structure.py` | — |
| Table declaration | `adapters/storage/postgres/schema.py`; `metadata = MetaData(schema="core")`, so tables in other schemas pass `schema="…"` explicitly | `documents`, `passages` tables |
| Migration | `adapters/storage/postgres/migrations/versions/NNN_<name>.py`; `revision` is the filename stem, `down_revision` the previous stem; `op.create_table(..., schema=…)`; raw `op.execute` only for what SQLAlchemy cannot express; downgrade mirrors in reverse | `008_passage_node.py`, `001_initial.py` (schema creation) |
| Domain model | `domain/<area>.py`, pydantic v2, `X` for rows and `XDraft` for inserts, validators on drafts | `domain/passages.py` (`Passage`, `PassageDraft`) |
| Composition | `composition.py:build_container` constructs repos and services and returns the `Container` dataclass; tools receive the whole container; `container.transaction_factory`, `container.verification` (a `QuoteVerifier`), `container.search`, `container.document_repo`, `container.passage_repo`, `container.settings` | the `QuoteVerifier(...)` and `HybridSearchService(...)` construction |
| Setting | one annotated field on `Settings`; env var is `RE_` + upper name; `research-engine config show` lists it automatically | `data_dir`, `plugins_dir`, `resolved_plugins_dir` |
| CLI | Typer; `cli/main.py` registers commands imperatively (`app.command("verify-quote")(verify_quote_command)`) and sub-apps with `app.add_typer`; a command builds the container, calls a service, and `finally: await container.close()` | `cli/verify.py` |
| Unit test | `tests/unit/...`, `pytestmark = pytest.mark.unit`, hand-built fakes, no database | `tests/unit/services/test_quote_verification.py` (`FakeTexts`, `FakePassages`, `FakeDocuments`) |
| Integration test | `tests/integration/...`, `pytestmark = [pytest.mark.integration]`; fixtures `engine` and `corpus` from `tests/integration/conftest.py`; `research_engine.testing.Corpus` creates and cleans up rows; `Corpus.track(table, id)` for rows that do not cascade from a document | `tests/integration/test_search_windows.py` (`_ingest` helper writes a document with canonical text and passages) |
| Repo surface guard | `tests/unit/adapters/test_repository_surface.py` holds an `EXPECTED` dict of repo class to method names; adding a repo method means updating it | — |

Two facts about existing code that shape this work:

- `core.documents.metadata` is `json`, not `jsonb`. Read keys with
  `metadata->>'edition_key'` in SQL, or `sa.cast(documents.c.metadata, JSONB)`
  as `repositories/documents.py` does. **Nothing in the engine writes a
  `edition_key` today**; Step 2 adds the way keys get onto documents.
- `core.passages.char_start` and `char_end` are nullable. Rows from older
  chunkers can lack offsets. A hit without offsets is not a citation draft, and
  the `source` block says so.

### 0.5 Test-isolation contract for the new schemas

`tests/integration/conftest.py` has an autouse session fixture that measures
every `core.*` table before and after the suite and fails if the corpus
changed. The new schemas (`evidence`, `argument`, `authored`, `bibliography`)
are outside that footprint. Two rules follow:

1. Extend `research_engine/testing/database.py`'s `CorpusFootprint` to include
   the new schemas as each migration lands, so the guard keeps its meaning.
2. Every integration test for this work creates its rows through `Corpus`
   helpers added in the same step (`add_span`, `add_work`, …) or registers
   them with `Corpus.track(table, id)`, and never truncates.

---

## Step 1 — Substrate: tests, the first real work, the committed contract

**Gate to enter this step.** None. This is the first step.

**Scope.** No migration and no new tool. Harden `verify_quote`, write one real
work by hand against the file contract, and commit the doc set so later steps
have a fixed reference. The point is the program doc's own discipline: three
real rows before a schema is written, one real work before a contract is
ratified.

**Non-goals.** No `work_*` tools yet. No changes to search. No Zotero import.

### 1.1 Audit and extend the `verify_quote` tests

The review stated that `verify_quote` had no automated tests. That is not the
state of the engine: `tests/unit/services/test_quote_verification.py` exists,
with classes `TestTiers`, `TestChunkStraddling`, `TestHonestAbsence`,
`TestNearMiss`, `TestWindowing`, built on `FakeTexts`, `FakePassages`,
`FakeDocuments` and a `_verifier()` helper. What is missing is an integration
test against real Postgres, because the `normalized` path
(`QuoteVerifier._locate_normalized`) uses `find_normalized` on the stored
`normalized_text` column and a raw-to-normalized length ratio to choose a
window, and none of that runs under the fakes.

Do this:

1. Check that each of these cases has a unit test; add any that is missing.
   - a quote differing only in typography (curly quotes, an em dash, a
     line-break hyphen) returns `normalized`, and `verified` is true;
   - the same quote never returns `exact`;
   - a quote with a changed word returns `near` with a `divergence` whose
     `source_continues` shows the source's continuation;
   - a quote crossing a passage boundary returns `exact` with
     `straddles_passages` true and two `passage_ids`;
   - a document with no canonical text returns `no_canonical_text`, not
     `not_found`, with `documents_checked == 0`;
   - an empty quote returns `not_found`.
2. Add `tests/integration/test_verify_quote.py`, marked `integration`, using
   the `_ingest` pattern from `tests/integration/test_search_windows.py`: one
   fixture document whose `TEXT` contains a curly-quoted sentence, an em dash,
   and a hyphenated line break, ingested through `PGDocumentTextRepo.put`
   (which writes `normalized_text`) and two `PassageDraft`s that split a
   sentence. Assert the six cases above against the real database, and that
   `location.char_start` / `char_end` slice `TEXT` to the matched text.
3. Record the fixture `TEXT` and its offsets in `tests/integration/fixtures/
   works/` as `lexicon_fixture.txt` with a sidecar `lexicon_fixture.json`
   listing the known spans (sentence bounds, the straddling span, the curly
   quote). Later steps reuse it.

### 1.2 Write the first real work by hand

Use only what exists: `find_passages`, `get_passage_context`, and the CLI
`research-engine verify-quote "<text>" --document-id <uuid> --json`.

1. Copy `works/_TEMPLATE.md` to the works directory as
   `deror-leviticus-25-translation.md`. The works directory is any folder
   outside the engine repo for now (decision 7); note its path, it becomes
   `RE_WORKS_DIR` in Step 2.
2. For every sentence that rests on a source: find it, read it, run the CLI
   with the document id, and copy the returned `char_start`, `char_end`, and
   `source_text` into an entry. Set `intent`. Set `edition_key` to the key the
   document will carry (Step 2 adds the way to store it; write it in the entry
   now). Put `[^cN]` in the prose.
3. Aim for at least six entries across at least two documents, with at least
   one `translation`, one `quotation`, one `background`, and one entry whose
   quote straddles a passage boundary.
4. Keep a scratch list of every field you wanted and the contract lacked, and
   every entry that came back `normalized` or `near`, with why. That list is
   the input to any contract change and goes in this guide's change log.

Do not write tools to make this easier. The friction is the measurement.

### 1.3 Commit the doc set

Copy `docs/design/*.md` and `works/README.md`, `works/_TEMPLATE.md` from the
satellite worktree into the engine repo under `docs/design/` and
`docs/design/works-contract/` on a branch `John-Cusack/works-phase0`, and
record the engine commit the docs were verified against in the guide's header.
Do not commit the real work file itself into the engine repo.

### 1.4 Tests

| Test | Asserts |
|---|---|
| unit, six cases | as listed in §1.1 |
| integration, six cases | same cases on real Postgres, plus offsets slice `TEXT` exactly |
| fixture sidecar | the JSON's spans match `TEXT` when sliced |

**Done when** both test files pass, the real work has at least six verified
entries with no `near` left unexplained, the scratch list of contract gaps is
in the change log, and the doc set is committed with the baseline commit
recorded.

---

## Step 2 — Phase 0: the file tools and the citation draft

**Gate to enter this step.** Step 1 is done: the `verify_quote` tests pass on
real Postgres, the first real work exists with verified entries, and the doc
set is committed.

**Scope.** No migration. One setting, one parser, three MCP tools plus their
CLI commands, one CLI command for edition keys, and two engine changes from
decision 6: the `source` block on search hits and the window hint on the verify
service. `work_index` and the mirror are Step 4 and are built only on their
trigger.

**Non-goals.** No database tables. No `authored.*`. No rendering beyond
markdown footnotes. No CSL. No LLM extraction of citations from prose.

### 2.1 The works directory (decision 7)

Add to `Settings` in `config/settings.py`, in the `# Paths` block:

```python
works_dir: Path | None = None   # RE_WORKS_DIR: the folder holding works/*.md, outside this repo
```

and a property `resolved_works_dir` that raises `WorksNotConfigured` (a new
`domain/errors.py` error) when unset. Every work tool and command reads paths
relative to it; `work_path` values are always relative to it and use forward
slashes. Files named `README.md` and `_TEMPLATE.md`, and any file starting with
`_`, are not works.

### 2.2 Edition keys on documents

Nothing writes `edition_key` today. Two additions:

- CLI `research-engine work set-key <document_id> <EDITION_KEY>` calling
  `PGDocumentRepo.update_metadata(doc_id, {"edition_key": key})`. Prints the
  document title and the key. Refuses (non-zero exit) if the document does not
  exist.
- The ingest convention, documented in the README of each pack and in
  `CHANGELOG.md`: a pack that knows its material's edition key writes it into
  the document draft's `metadata["edition_key"]`. No core code enforces this;
  `work_verify` reports its absence per citation.

`edition_key` is read everywhere with `metadata->>'edition_key'` (the column is
`json`, not `jsonb`).

### 2.3 The file contract parser

New module `services/works/files.py`, pure functions plus pydantic models in
`domain/works_files.py`:

```python
class CitationEntry(BaseModel):
    id: str                      # ^c[0-9]+$
    intent: Intent               # StrEnum of the eight target values
    role: Role | None = None     # asserts | supports | rebuts | context
    document_id: UUID
    char_start: int              # >= 0
    char_end: int                # > char_start
    quoted_text: str             # non-empty
    edition: str | None = None
    edition_key: str | None = None
    locator: dict[str, Any] = Field(default_factory=dict)

class WorkFrontMatter(BaseModel):
    work: str                    # W-001
    title: str
    type: WorkType               # translation | essay | dossier | script | outline
    status: WorkStatus           # draft | review | published
    created: date
    claims: list[str] = Field(default_factory=list)
    citations: list[CitationEntry] = Field(default_factory=list)

class WorkFile(BaseModel):
    work_path: str               # relative to works_dir
    front_matter: WorkFrontMatter
    front_matter_sha: str        # sha256 hex of the raw YAML block bytes, fences excluded
    body: str
    markers: list[str]           # every [^cN] in the body, in order, excluding definition lines
    entry_errors: list[EntryError]   # per-entry validation failures, not exceptions
```

`parse_work_file(path: Path, works_dir: Path) -> WorkFile`. Rules: the file
starts with `---\n`, the YAML block ends at the next `\n---\n`; parse with
`yaml.safe_load`; validate the header strictly (a bad header is a hard error);
validate each citation entry individually, collecting failures into
`entry_errors` so one bad entry does not hide the others; markers are
`re.findall(r"\[\^(c\d+)\]", body_without_definition_lines)` where definition
lines match `^\[\^c\d+\]:` at line start.

### 2.4 `work_verify`

Service `services/works/verify.py`, class `WorkVerifier(document_texts,
documents, passages, verification, works_dir)`. Tool `mcp/tools/work_verify.py`
and CLI `research-engine work verify [path] [--gate review|publish] [--json]`.

Per work: parse the file, then per entry, in this order, stopping the entry's
checks at the first hard failure:

| # | Check | Finding on failure | Severity |
|---|---|---|---|
| 1 | entry validates against `CitationEntry` | `AUTH_ENTRY_INVALID` (with the pydantic message) | error |
| 2 | `documents.get(document_id)` exists | `AUTH_DOCUMENT_UNKNOWN` | error |
| 3 | `document_texts.lengths(document_id)` is not None | `AUTH_SOURCE_UNCHECKABLE` | error |
| 4 | `verification.verify(quoted_text, document_id, window=(char_start, char_end))` | `near` or `not_found` → `AUTH_QUOTE_UNVERIFIED` with the tier and divergence in `detail` | error |
| 5 | returned `char_start`/`char_end` equal the entry's | `AUTH_SOURCE_SPAN_STALE` with both spans in `detail` (the text moved under the entry: re-parse drift or a hand-typed offset) | error |
| 6 | region rule for the intent (§2.7c) | `AUTH_SPAN_NOT_NARROWED` (quotation, translation) / `AUTH_SPAN_REGION` (support, source, definition) | error / warning |
| 7 | `edition_key` or `edition` present | `AUTH_CITATION_EDITION_MISSING` | warning at review, error at publish |
| 8 | `edition_key` equals the document's `metadata.edition_key` | absent on the document → `AUTH_EDITION_KEY_UNKNOWN` (warning); present and different → `AUTH_EDITION_KEY_MISMATCH` (error) | as stated |
| 9 | entry id appears as a marker in the body | `AUTH_CITATION_MARKER_MISSING` | warning |

Per work, after the entries: every marker has an entry, else
`AUTH_CITATION_MARKER_DANGLING` (error); every `claims:` ref resolves against
`argument.claims.ref` once 010 exists, else `AUTH_CLAIM_UNRESOLVED` (info
before 010, warning after, error at the publish gate after 010). Status-based
blocking on claims (a work resting on a rebutted premise) is the reasoner's
job in the program doc and is not implemented here.

Gates: `review` fails on any error. `publish` fails on any error plus
`AUTH_CITATION_EDITION_MISSING` and, after 010, `AUTH_CLAIM_UNRESOLVED`. The
work's own `status:` field is compared with the gate result: a file that says
`published` and fails the publish gate gets `AUTH_STATUS_UNEARNED` (error).

Output:

```json
{
  "works": [{
    "work_path": "deror-leviticus-25-translation.md",
    "work": "W-001", "status": "draft",
    "citations": [{"id": "c1", "intent": "quotation", "tier": "normalized",
                   "document_id": "…", "char_start": 3326, "char_end": 3546,
                   "edition_key": "TDNT_1964", "findings": ["AUTH_SPAN_REGION"]}],
    "findings": [{"rule_id": "AUTH_QUOTE_UNVERIFIED", "severity": "error",
                  "citation_id": "c3", "message": "…", "detail": {…}}],
    "gate": {"name": "review", "passed": false, "blockers": ["AUTH_QUOTE_UNVERIFIED"]}
  }],
  "summary": {"works": 1, "citations": 6, "errors": 1, "warnings": 2}
}
```

The CLI prints a table and exits non-zero when a gate was requested and
failed. Rule ids are the contract; messages are for humans.

### 2.5 `work_citations`

Tool `mcp/tools/work_citations.py` and CLI `research-engine work citations
(--document <uuid> | --edition-key <key> | --claim <ref>)`. Exactly one selector.
Implementation in Phase 0: parse every work file under `works_dir` and filter
entries (or `claims:` refs) by the selector. Output:

```json
{"matches": [{"work_path": "…", "work": "W-001", "title": "…", "status": "draft",
              "citation_id": "c1", "intent": "quotation", "document_id": "…",
              "char_start": 3326, "char_end": 3546, "quoted_text": "…"}],
 "source": "files"}
```

When the Step 4 mirror exists the same tool queries it and reports
`"source": "mirror"`; detect by a container flag set in `build_container`
when the `core.works_index` table exists.

### 2.6 `work_render`

Tool `mcp/tools/work_render.py` and CLI `research-engine work render <path>
[--out <file>]`. Output is the body with footnote definitions appended, one
per entry in id order:

```
[^c1]: Kittel, *Theological Dictionary of the New Testament*, vol. II (1964), p. 64. [normalized]
```

Author, title and year come from document metadata keys `author`, `title`
(falling back to `documents.title`), `year` or `date`. Any missing part is
rendered as `document <id>` and the footnote is tagged `[provisional]`. The
tier tag is always present. This is the only place display strings are built;
they are never written back to the file.

### 2.7 Engine changes for the citation draft (decision 6)

**(a) The `source` block on hits.** `PassageHit` in `domain/passages.py`
gains `source: HitSource | None = None` with

```python
class HitSource(BaseModel):
    document_title: str | None
    edition_key: str | None
    edition: str | None            # metadata.edition if a pack wrote it
    parser_version: str | None     # document_texts.parser_version; None when no canonical text
    has_canonical_text: bool
    has_offsets: bool              # passage char_start/char_end are not NULL
```

Populate it in `HybridSearchService._hydrate` with **one batched query per
result page**, never per hit: add `PGDocumentRepo.get_many(ids) ->
list[Document]` and `PGDocumentTextRepo.parser_versions(ids) -> dict[UUID,
str]`, inject a small `HitSourceReader(documents, document_texts)` into
`HybridSearchService` the way `PassageWindowReader` is injected, and wire it
in `composition.py`. `find_passages` emits `"source": h.source.model_dump()`
per hit. Update `tests/unit/adapters/test_repository_surface.py` for the two
new repo methods.

**(b) The window hint on the verify service.** Change

```python
async def verify(self, quote: str, document_id: UUID | None = None,
                 *, window: tuple[int, int] | None = None) -> QuoteVerification
```

When `window` and `document_id` are both given: `lo = max(0, start - slack)`,
`hi = min(raw_len, end + slack)` with `slack = len(quote) + 256`; read
`get_span(document_id, lo, hi)`; try `window_text.find(quote)` → `EXACT`;
else `_find_folded(window_text, match_form)` → `NORMALIZED`; offset the span by
`lo` and hand it to `_resolve` as today. On a miss, fall through to the
existing whole-document path unchanged. The MCP tool `verify_quote` gains an
optional `window: {char_start, char_end}` object (validated in the handler,
since dispatch does not validate nested objects). Add a `TestWindow` class to
`tests/unit/services/test_quote_verification.py` asserting: a quote copied
from a hit resolves inside the window and `find_raw` is never called; a
whitespace-differing copy returns `normalized`; a quote absent from the window
falls through and is found by the whole-document path.

**(c) The region rule.** A span is a *region* when a passage row of the
document's current chunker has exactly its `[char_start, char_end]`. Use
`PGPassageRepo.covering_span(document_id, start, end)` and compare bounds.
`quotation` and `translation`: a region, or a span longer than
`MAX_QUOTE_CHARS = 1000`, is `AUTH_SPAN_NOT_NARROWED` (error). `support`,
`source`, `definition`: a region is `AUTH_SPAN_REGION` (warning). `see_also`,
`background`, `contrast`: a region is fine. These are the core defaults; a
work-type policy overrides them in Step 6.

### 2.8 Wiring

- `composition.py`: construct `WorkVerifier`, `WorkFileReader`, and
  `WorkRenderer` when `settings.works_dir` is set; add them to `Container` as
  `work_files`, `work_verifier`, `work_renderer`. Tools that find the field
  `None` return `{"error": {"code": "works_not_configured", …}}`.
- `mcp/dispatch.py`: import the three tool modules and append to
  `CORE_TOOL_MODULES`.
- `cli/work.py`: a Typer sub-app `work_app` with `verify`, `citations`,
  `render`, `set-key`; `cli/main.py`: `app.add_typer(work_app, name="work")`.
- `CHANGELOG.md`: one section for the tools, one for the `source` block and
  the window hint, since both change a tool's response.

### 2.9 Tests

| Test | Kind | Asserts |
|---|---|---|
| parser: good file | unit | the fixture work file parses to the expected models; sha is stable; markers are found in order and definition lines are excluded |
| parser: bad entries | unit | one invalid entry yields one `AUTH_ENTRY_INVALID` and the other entries still parse |
| verify: each finding | unit, fakes | one fixture entry per rule in §2.4 produces exactly that rule id |
| verify: gates | unit | `review` and `publish` compute blockers as specified; `AUTH_STATUS_UNEARNED` fires |
| verify: real corpus | integration | a fixture document (Step 1 sidecar) plus a temp works dir with a fixture work: `exact`, `normalized`, `near`, `not_found`, straddle, region, edition mismatch all report as designed |
| citations | integration | `--document`, `--edition-key`, `--claim` each return the fixture entry and nothing else |
| render | unit | footnote text for a document with and without metadata; provisional tag present exactly when a part is missing |
| set-key | integration | the key is on the document afterwards; `work_verify` stops reporting `AUTH_EDITION_KEY_UNKNOWN` |
| source block | integration | a `find_passages` hit for the fixture carries the key, the parser version, `has_canonical_text` true, `has_offsets` true; one query per page (assert with a statement counter) |
| window hint | unit + integration | as in §2.7b |
| tier honesty | integration | `work_verify` reports `normalized` for the curly-quote entry, never `exact` |

**Done when** the real work from Step 1 passes `work verify --gate review`
with every remaining finding explained, `work citations --document` finds it,
`work render` produces its footnotes, a `find_passages` hit shows the `source`
block, and the test table is green under `make test` and
`make test-integration`.

---

## Step 3 — Migrations 009 and 010: the span table and the ledger

**Gate to enter this step.** Step 1 and Step 2 are complete: `verify_quote` has
tier-honesty tests, one real work verifies by hand, the Phase 0 tools pass their
tests, and the doc set is committed. Do not start here first: the point of the
order is that schema defects surface while they are still a doc edit.

**Scope.** Two additive Alembic migrations, one repository, one service change.
009 creates `evidence.source_spans`. 010 creates `argument.claims`,
`argument.claim_edges` and `argument.anchors` with the anchor revised to
reference the span. The span resolver is the shared write path both
`claim_upsert` (program doc §4) and, later, `work_cite` use. Nothing in this
step touches `authored.*`.

**Non-goals.** No `argument.derivations` unless master §11 item 5 has been
decided yes (default: omit). No `claim_phrasings`. No `verify_attempts` yet: its
DDL is below so it is a transcription job when the first post-flip failure
needs it, but decision 4 says first need, and this step is not it.

### 3.1 Migration 009 — `evidence_source_spans`

```sql
CREATE SCHEMA evidence;

CREATE TABLE evidence.source_spans (
    id                    uuid PRIMARY KEY,
    document_id           uuid NOT NULL
                          REFERENCES core.documents(id) ON DELETE RESTRICT,
    char_start            integer NOT NULL,
    char_end              integer NOT NULL,
    -- The canonical slice document_texts.text[char_start:char_end], written by
    -- the resolver. NOT the author's typed quote: that lives on the citing row.
    quoted_text           text NOT NULL,
    parser                text,
    parser_version        text,          -- copied from document_texts; a mismatch marks the span stale
    passage_id            uuid REFERENCES core.passages(id) ON DELETE SET NULL,  -- cache only
    created_at            timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT source_spans_range_ck CHECK (char_start >= 0 AND char_end > char_start),
    -- Span identity: one row per (document, coordinates). The unique index
    -- also serves range lookups; no separate index.
    CONSTRAINT source_spans_coordinates_uk UNIQUE (document_id, char_start, char_end)
);
```

Downgrade drops the table and the schema. Nothing references the table yet, so
the downgrade is clean.

**Deferred, transcribe when decision 4's trigger fires** (a separate later
migration, not part of 009):

```sql
CREATE TABLE evidence.verify_attempts (
    id            uuid PRIMARY KEY,
    document_id   uuid REFERENCES core.documents(id) ON DELETE CASCADE,  -- NULL for corpus-wide misses
    quote         text NOT NULL,
    tier          text NOT NULL,      -- near | not_found | no_canonical_text
    actor         text NOT NULL,
    context       jsonb NOT NULL DEFAULT '{}',  -- the work, block or claim being cited
    attempted_at  timestamptz NOT NULL DEFAULT now()
);
```

### 3.2 Migration 010 — `argument`

Program doc §2 verbatim for `claims` and `claim_edges`; `anchors` revised per
master §4 (decision 1). The anchor keeps its own typed quote and tier and swaps
its three coordinate columns for the span id. `passage_id` leaves the anchor:
the span carries that cache.

```sql
CREATE SCHEMA argument;

CREATE TABLE argument.claims (
    id                 uuid PRIMARY KEY,
    ref                text NOT NULL UNIQUE,          -- 'JUB-004', the handle you cite
    statement          text NOT NULL,
    kind               text NOT NULL,                 -- opposition | mine | premise | lexical
    status             text NOT NULL DEFAULT 'open',
    confidence         real,
    steelman           text,
    public_ready       boolean NOT NULL DEFAULT false,
    academic_candidate boolean NOT NULL DEFAULT false,
    attributes         jsonb NOT NULL DEFAULT '{}',
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT claims_status_ck CHECK (status IN
        ('open','researching','rebutted','weakened','unresolved','conceded')),
    CONSTRAINT claims_conf_ck CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1)
);

CREATE TABLE argument.claim_edges (
    id         uuid PRIMARY KEY,
    source_id  uuid NOT NULL REFERENCES argument.claims(id) ON DELETE CASCADE,
    target_id  uuid NOT NULL REFERENCES argument.claims(id) ON DELETE RESTRICT,
    relation   text NOT NULL,   -- depends_on|supports|contradicts|refines|rebuts|concedes
    confidence real,
    note       text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_id, target_id, relation),
    CONSTRAINT claim_edges_no_self CHECK (source_id <> target_id)
);
CREATE INDEX claim_edges_target_idx ON argument.claim_edges (target_id, relation);
CREATE INDEX claim_edges_source_idx ON argument.claim_edges (source_id, relation);

CREATE TABLE argument.anchors (
    id               uuid PRIMARY KEY,
    claim_id         uuid NOT NULL REFERENCES argument.claims(id) ON DELETE CASCADE,
    role             text NOT NULL,   -- asserts | supports | rebuts | context
    person_entity_id uuid REFERENCES core.entities(id) ON DELETE SET NULL,

    -- Decision 1: the address is the shared span; the quote and tier are this row's.
    source_span_id   uuid NOT NULL REFERENCES evidence.source_spans(id) ON DELETE RESTRICT,
    quoted_text      text NOT NULL,   -- as typed by the citer
    verify_status    text,            -- exact | normalized | near (never not_found)
    verified_at      timestamptz,
    parser_version   text,            -- the parser version the tier was computed against

    edition          text,
    edition_key       text,
    locator          jsonb NOT NULL DEFAULT '{}',
    created_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT anchors_role_ck CHECK (role IN ('asserts','supports','rebuts','context')),
    CONSTRAINT anchors_verify_ck CHECK
      (verify_status IS NULL OR verify_status IN ('exact','normalized','near'))
);
CREATE INDEX anchors_claim_idx ON argument.anchors (claim_id);
CREATE INDEX anchors_span_idx  ON argument.anchors (source_span_id);
```

Optional, only if master §11 item 5 is decided yes (default omit):

```sql
CREATE TABLE argument.derivations (
    id            uuid PRIMARY KEY,
    run_id        uuid NOT NULL,
    rule          text NOT NULL,
    claim_id      uuid NOT NULL REFERENCES argument.claims(id) ON DELETE CASCADE,
    conclusion    text NOT NULL,      -- undermined | unsound | unsafe | strawman_risk | ...
    justification jsonb NOT NULL,     -- which rule fired on which facts
    created_at    timestamptz NOT NULL DEFAULT now()
);
```

### 3.3 The span resolver

One class, one method, used by every writer of a span. It is the whole
difference between an identity join and span-overlap geometry.

```
SourceSpanRepo.resolve(conn, *, document_id, char_start, char_end) -> SourceSpan
```

Behaviour, normative (master §4):

1. `SELECT id, parser_version FROM evidence.source_spans WHERE document_id = $1
   AND char_start = $2 AND char_end = $3`. On a hit, return it.
2. On a miss, read the canonical slice `document_texts.text[char_start:char_end]`
   and the document's `parser` / `parser_version`, then `INSERT ... ON CONFLICT
   (document_id, char_start, char_end) DO NOTHING`, then re-select. The re-select
   handles the race between two writers in different transactions.
3. The slice is stored as `quoted_text`. The caller never passes its own text
   for that column.
4. `passage_id`: the best-overlap passage for the current chunker version
   (program doc §2's recovery query). Optional; may be NULL.
5. The resolver runs inside the caller's transaction. It never commits.

Staleness is a query, not a column: a span is stale when its `parser_version`
is distinct from its document's current `document_texts.parser_version`.

```sql
-- Stale spans, discovered in one table.
SELECT s.id
FROM evidence.source_spans s
JOIN core.document_texts dt ON dt.document_id = s.document_id
WHERE s.parser_version IS DISTINCT FROM dt.parser_version;
```

### 3.4 `claim_upsert` contract (program doc §4, restated for the resolver)

This guide does not specify the ledger tools; the program doc does. It fixes
one thing: `claim_upsert` calls the verify service for each anchor, refuses
`not_found` and `no_canonical_text` (nothing stored, the attempt logged once
`verify_attempts` exists), resolves the span through `SourceSpanRepo.resolve`
with the coordinates verification returned, and writes the anchor with the
typed quote, the tier, `verified_at`, and the document's `parser_version`.
`near` is stored as `near`; the freeze gate, not the write, decides whether a
waiver is needed.

### 3.5 Tests for this step

| Test | Asserts |
|---|---|
| resolver idempotency | two `resolve` calls with identical coordinates in two transactions return one id; the table has one row |
| resolver race | two concurrent transactions inserting the same coordinates both return the same id (use two connections, commit both) |
| canonical slice | `quoted_text` equals `document_texts.text[start:end]` byte for byte, regardless of what the caller quoted |
| staleness query | bump a document's `parser_version` in a fixture; the stale query returns exactly that document's spans |
| RESTRICT | deleting a document with a span fails; deleting the span first succeeds |
| anchor shape | an anchor row cannot be inserted with `verify_status = 'not_found'` |
| migration round trip | `alembic upgrade head` then `downgrade` twice leaves no `evidence` or `argument` schema behind |

**Done when** both migrations apply and revert cleanly on the dev database, the
resolver tests pass, and `claim_upsert` (if built in the same week per the
program doc) writes anchors that join to spans by id.

---

## Step 4 — Migration 011: the bridge mirror (conditional)

**Gate to enter this step.** Only if the trigger has fired before Phase 1
starts: more than three works or more than twenty citations in the works
directory, and `grep` has stopped being enough. If the trigger has not fired,
skip this step entirely. If Phase 1 lands first, this migration is never
written.

**Scope.** One additive migration and the `work_index` tool. The mirror is a
rebuildable projection of front matter. It creates no truth; it can be dropped
and rebuilt at any time; and it is dropped, with `work_index`, when the last
pre-012 work has flipped (F3).

### 4.1 Migration 011 — `work_citations`

Bridge doc §4 with three amendments: `work_path` is relative to the configured
works root (decision 7); `intent` is required and `role` optional (decision 11);
`verify_status` keeps `not_found`, because the mirror records failures and the
gate refuses them.

```sql
CREATE TABLE core.works_index (
  work_path        text PRIMARY KEY,      -- relative to settings.works_dir, e.g. 'deror-leviticus-25-translation.md'
  work_ref         text NOT NULL,         -- front-matter `work:`, e.g. W-001
  title            text NOT NULL,
  work_type        text NOT NULL,         -- translation | essay | dossier | script | outline
  status           text NOT NULL,         -- draft | review | published
  claim_refs       text[] NOT NULL DEFAULT '{}',
  front_matter_sha text NOT NULL,         -- drift detection
  indexed_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE core.work_citations (
  work_path     text NOT NULL REFERENCES core.works_index(work_path) ON DELETE CASCADE,
  citation_id   text NOT NULL,            -- 'c1', the [^c1] handle
  intent        text NOT NULL,            -- decision 11
  role          text,                     -- optional; only when the entry also backs a claim ref
  document_id   uuid NOT NULL REFERENCES core.documents(id) ON DELETE RESTRICT,
  char_start    integer NOT NULL,
  char_end      integer NOT NULL,
  quoted_text   text NOT NULL,
  verify_status  text NOT NULL,           -- exact | normalized | near | not_found | no_canonical_text
  parser_version text,
  edition_key    text,
  edition       text,
  locator       jsonb NOT NULL DEFAULT '{}',
  PRIMARY KEY (work_path, citation_id),
  CONSTRAINT wc_span_ck CHECK (char_end > char_start)
);
CREATE INDEX wc_document_idx ON core.work_citations (document_id);
CREATE INDEX wc_edition_key_idx   ON core.work_citations (edition_key);
```

Note: the mirror anchors inline by coordinates, deliberately. It does not
reference `evidence.source_spans`; it is a cache of files, not a consumer of the
span table, and it is dropped at the flip.

### 4.2 `work_index`

Input: `{ "path": "<relative>.md" }` or nothing for all works. Behaviour: parse
front matter, compute `front_matter_sha` over the YAML block bytes, upsert the
work row and replace its citation rows in one transaction, run the verify
service per entry and store the tier as returned. Idempotent: a second run with
unchanged files changes nothing. Output:

```json
{ "indexed": 3, "unchanged": 2, "drift": ["essay-2.md"], "failures": [] }
```

`work_citations` switches from scanning files to querying the mirror when the
tables exist; `work_verify` reports `AUTH_INDEX_STALE` when a file's current
`front_matter_sha` differs from the row's.

### 4.3 Tests

Bridge appendix #2, #3, #4: index mirror (fixture work yields exactly its
front-matter rows; second run changes nothing), RESTRICT (deleting a cited
document fails while rows exist), drift (editing front matter without
re-indexing flips the sha and `work_verify` reports the work stale).

---

## Step 5 — The flip rehearsal (F6)

**Gate to enter this step.** Migration 012 and the Phase 1 tools (Step 6) exist
in a branch, and no real work has been frozen. The rehearsal runs before the
first real freeze, on one real work, and its output is discarded.

**Purpose.** The port has three heuristics that have never run against a real
file: block boundaries from markdown structure, the marker rewrite, and the
entry-to-row mapping. The rehearsal tests all three while a defect is still a
code edit rather than a data event.

### 5.1 Procedure

1. Pick the work. The Lev 25 / *deror* translation is the candidate.
2. `work_verify --gate review` must pass on it first. A work that would fail
   the review gate is not a rehearsal; it is a debugging session.
3. In one transaction: `work_create` from the front matter; one
   `work_block_upsert` per block per §5.2; one `work_cite` per entry per §5.3;
   one `work_link` per claim ref worth typing (entity links only; claim links
   are Phase B). Store the original front matter verbatim in
   `work_revisions.metadata.port.front_matter` so nothing is lost.
4. `work export --draft` the new draft revision to a scratch path.
5. Diff the export against the original file per §5.4.
6. Roll the transaction back, or delete the work. Nothing from the rehearsal
   survives.
7. Record the diff and every defect found in the change log of this guide.

### 5.2 Block boundaries (fixed by this guide)

The markdown body after the front matter is split into blocks as follows.

| Markdown | Block | `block_type` | Parent |
|---|---|---|---|
| ATX heading `#`..`######` | one block; heading text is `title`, body empty | `heading` | nearest heading of lower level, else root |
| paragraph (text between blank lines) | one block | `paragraph` | the nearest preceding heading |
| fenced code block | one block, fence included | `code` | the nearest preceding heading |
| list (consecutive list lines) | one block for the whole list | `list` | the nearest preceding heading |
| blockquote (consecutive `>` lines) | one block | `quotation` | the nearest preceding heading |
| `[^cN]: ...` footnote definitions | not a block; dropped, since footnotes are rendered from rows | — | — |
| HTML comment `<!-- block:<uuid> -->` on its own line | sets the `block_key` of the next block (used by import, Step 6 §6.3) | — | — |

Position is the 0-based order among siblings. `block_key` is a fresh UUID at
port unless a `<!-- block:… -->` comment precedes the block.

### 5.3 Entry-to-row mapping

| Front-matter field | Row and column |
|---|---|
| `id` (`c1`) | `citation_occurrences.citation_key` = uuid5(work_id, "c1"), deterministic so a re-port is idempotent; the port logs `c1 → key` |
| `intent` | `citation_occurrences.intent` |
| `role` | kept in `work_revisions.metadata.port.front_matter`; becomes a `block_claim_links` relation in Phase B; not written now |
| `document_id`, `char_start`, `char_end` | resolved through `SourceSpanRepo.resolve` → `citation_items.source_span_id` |
| `quoted_text` | `citation_items.quoted_text`, re-verified at port; the returned tier → `verify_status`, now() → `verified_at` |
| `edition_key` | `citation_items.edition_key`; `edition_id` set when `bibliography.editions` has that key |
| `edition` | `citation_items.locator.edition` (a locator key, not a column) |
| `locator` | `citation_items.locator`, merged with the above |
| occurrence placement | `inline` if `[^cN]` appears in the body, else `block_end` on the last block, and `AUTH_CITATION_MARKER_MISSING` is reported |

An entry with neither `edition_key` nor `edition` fails the §5 CHECK. The port
refuses to start on such a work; `work_verify` reports them as
`AUTH_CITATION_EDITION_MISSING` long before, which is the point.

### 5.4 The acceptable diff

`work export --draft` regenerates the file from rows. Compared to the original:

- Front matter: entries are re-emitted in citation order with the same fields
  plus `citation_key`; field order may change; values must not.
- Body: the only permitted change is `[^cN]` → `{{cite:<citation_key>}}` and
  the removal of `[^cN]:` footnote definition lines. Block comments
  `<!-- block:<uuid> -->` are added before each block.
- Anything else in the diff, including whitespace inside a paragraph, is a
  port bug. Fix the port, not the file.

### 5.5 Done when

The rehearsal diff is markers-only on the real work, every defect is fixed in
code, and the same rehearsal re-run produces the same diff. Only then is the
first real freeze allowed.

---

## Step 6 — Migration 012 and the Phase 1 spine

**Gate to enter this step.** Steps 1 through 3 are done and merged; Step 4 was
done or skipped by its trigger; Step 5's rehearsal has run on a branch of this
step's code and its diff is markers-only. The first real freeze happens at the
end of this step, not before.

**Scope (decisions 5 and 8).** One additive migration (`authored.*` plus the `bibliography.editions`
stub), six repositories, five services, eight MCP tools, and two CLI commands
for the drafting loop. `block_claim_links` is Phase B and not here.
`work_export` with a manifest is deferred to first publication; the draft
export in the drafting loop is markdown only and writes no manifest.

**Non-goals.** No FTS or embeddings over blocks. No project membership. No
CSL rendering. No claim links. No DOCX or LaTeX.

### 6.1 Migration 012 — `authored_and_bibliography`

Every table below is declared in `schema.py` with an explicit `schema=`
argument, because the shared `MetaData` defaults to `core`. The migration
creates both schemas with `op.execute("CREATE SCHEMA IF NOT EXISTS …")` first,
as `001_initial` does for `core`, then `op.create_table(..., schema=…)`.
Downgrade drops tables in reverse dependency order, then both schemas.

```sql
CREATE SCHEMA authored;
CREATE SCHEMA bibliography;

-- Decision 12: the stub. P3-1 extends it and turns the document join into a FK.
CREATE TABLE bibliography.editions (
    id          uuid PRIMARY KEY,
    edition_key  text NOT NULL UNIQUE,
    csl         jsonb NOT NULL DEFAULT '{}',
    created_at  timestamptz NOT NULL DEFAULT now()
);
-- Backfill in the same migration, then maintained at ingest (§6.6):
INSERT INTO bibliography.editions (id, edition_key)
SELECT gen_random_uuid(), DISTINCT_KEYS.k
FROM (SELECT DISTINCT metadata->>'edition_key' AS k FROM core.documents
      WHERE metadata->>'edition_key' IS NOT NULL) AS DISTINCT_KEYS
ON CONFLICT (edition_key) DO NOTHING;

CREATE TABLE authored.works (
    id                  uuid PRIMARY KEY,
    slug                text NOT NULL UNIQUE,
    title               text NOT NULL,
    work_type           text NOT NULL,          -- translation | essay | dossier | script | outline | pack-defined
    status              text NOT NULL DEFAULT 'draft',
    language            text,
    abstract            text,
    current_revision_id uuid,
    metadata            jsonb NOT NULL DEFAULT '{}',
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    archived_at         timestamptz,
    CONSTRAINT works_status_ck CHECK (status IN ('draft','review','published','archived'))
);

CREATE TABLE authored.work_revisions (
    id                 uuid PRIMARY KEY,
    work_id            uuid NOT NULL REFERENCES authored.works(id) ON DELETE CASCADE,
    revision_number    integer NOT NULL,
    parent_revision_id uuid REFERENCES authored.work_revisions(id) ON DELETE RESTRICT,
    state              text NOT NULL DEFAULT 'draft',
    message            text,
    content_hash       bytea,
    created_by         text NOT NULL DEFAULT 'user',
    created_at         timestamptz NOT NULL DEFAULT now(),
    frozen_at          timestamptz,
    published_at       timestamptz,
    metadata           jsonb NOT NULL DEFAULT '{}',   -- port keeps the original front matter under metadata.port
    UNIQUE (id, work_id),
    UNIQUE (work_id, revision_number),
    CONSTRAINT work_revision_state_ck CHECK (state IN ('draft','frozen','published','superseded'))
);

ALTER TABLE authored.works
  ADD CONSTRAINT works_current_revision_fk
  FOREIGN KEY (current_revision_id, id)
  REFERENCES authored.work_revisions(id, work_id)
  DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE authored.work_blocks (
    id              uuid PRIMARY KEY,
    revision_id     uuid NOT NULL REFERENCES authored.work_revisions(id) ON DELETE CASCADE,
    block_key       uuid NOT NULL,
    parent_id       uuid,
    position        integer NOT NULL,
    block_type      text NOT NULL,              -- heading | paragraph | quotation | translation_unit | code | list | footnote | table | figure_caption | pack-defined
    title           text,
    body_markdown   text NOT NULL DEFAULT '',
    attributes      jsonb NOT NULL DEFAULT '{}',
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, revision_id),
    UNIQUE (revision_id, block_key),
    UNIQUE NULLS NOT DISTINCT (revision_id, parent_id, position),   -- PG15; dev server is pg15
    FOREIGN KEY (parent_id, revision_id)
      REFERENCES authored.work_blocks(id, revision_id) ON DELETE RESTRICT
);
CREATE INDEX work_blocks_revision_idx ON authored.work_blocks (revision_id, parent_id, position);

CREATE TABLE authored.citation_occurrences (
    id              uuid PRIMARY KEY,
    citation_key    uuid NOT NULL,
    block_id        uuid NOT NULL REFERENCES authored.work_blocks(id) ON DELETE CASCADE,
    placement       text NOT NULL DEFAULT 'inline',
    intent          text NOT NULL DEFAULT 'source',
    note            text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (block_id, citation_key),
    CONSTRAINT citation_placement_ck CHECK (placement IN ('inline','block_end')),
    CONSTRAINT citation_intent_ck CHECK
      (intent IN ('source','support','contrast','background','definition','translation','quotation','see_also'))
);

CREATE TABLE authored.citation_items (
    occurrence_id   uuid NOT NULL REFERENCES authored.citation_occurrences(id) ON DELETE CASCADE,
    position        integer NOT NULL,
    edition_id      uuid REFERENCES bibliography.editions(id) ON DELETE RESTRICT,   -- nullable until P3 backfills
    edition_key      text,
    source_span_id  uuid REFERENCES evidence.source_spans(id) ON DELETE RESTRICT,
    quoted_text     text,                       -- decision 1: the typed quote, per citer
    verify_status   text,                       -- NULL | exact | normalized | near
    verified_at     timestamptz,
    locator         jsonb NOT NULL DEFAULT '{}',
    prefix          text,
    suffix          text,
    suppress_author boolean NOT NULL DEFAULT false,
    PRIMARY KEY (occurrence_id, position),
    CONSTRAINT citation_identity_ck CHECK (edition_id IS NOT NULL OR edition_key IS NOT NULL),
    CONSTRAINT citation_verify_ck CHECK (verify_status IS NULL OR verify_status IN ('exact','normalized','near')),
    CONSTRAINT citation_quote_needs_span_ck CHECK (quoted_text IS NULL OR source_span_id IS NOT NULL)
);
CREATE INDEX citation_items_span_idx    ON authored.citation_items (source_span_id);
CREATE INDEX citation_items_edition_idx ON authored.citation_items (edition_id);
CREATE INDEX citation_items_edition_key_idx  ON authored.citation_items (edition_key);

CREATE TABLE authored.block_source_links (
    block_id       uuid NOT NULL REFERENCES authored.work_blocks(id) ON DELETE CASCADE,
    source_span_id uuid NOT NULL REFERENCES evidence.source_spans(id) ON DELETE RESTRICT,
    relation       text NOT NULL,               -- quotes | paraphrases | translates | summarizes | discusses | supports | contrasts | defines
    confidence     real,
    note           text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (block_id, source_span_id, relation),
    CONSTRAINT block_source_conf_ck CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1)
);
CREATE INDEX block_source_links_span_idx ON authored.block_source_links (source_span_id);

-- Decision 9: ships with 012.
CREATE TABLE authored.block_entity_links (
    block_id     uuid NOT NULL REFERENCES authored.work_blocks(id) ON DELETE CASCADE,
    entity_id    uuid NOT NULL REFERENCES core.entities(id) ON DELETE RESTRICT,
    relation     text NOT NULL,                 -- renders | discusses
    surface_form text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (block_id, entity_id, relation)
);
CREATE INDEX block_entity_links_entity_idx ON authored.block_entity_links (entity_id, relation);

-- Name fixed by this guide (no doc named it). Waivers are rows, never flags.
CREATE TABLE authored.waivers (
    id           uuid PRIMARY KEY,
    revision_id  uuid NOT NULL REFERENCES authored.work_revisions(id) ON DELETE CASCADE,
    rule_id      text NOT NULL,
    subject      text,                          -- citation_key, block_key, or NULL for the whole revision
    actor        text NOT NULL,
    reason       text NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX waivers_revision_idx ON authored.waivers (revision_id, rule_id);
```

`gen_random_uuid()` is in core Postgres 13+; the dev server is 15. If the
project prefers application-side UUIDs everywhere, do the editions backfill in
Python inside the migration instead.

### 6.2 Repositories

Follow the engine pattern exactly: `PG<Thing>Repo(engine: AsyncEngine)`;
writes take `tx: Transaction` as the first parameter and never commit; reads
open their own connection. Declare the table objects in `schema.py` with
`schema="authored"`, `schema="bibliography"`, `schema="evidence"`. Add a
Protocol per repo in `ports/repositories.py` and re-export from
`repositories/__init__.py` with `__all__`.

| Repository | Methods (all async) |
|---|---|
| `PGSourceSpanRepo` (Step 3) | `resolve(tx, *, document_id, char_start, char_end) -> SourceSpan`; `get(span_id)`; `stale(limit) -> list[SourceSpan]`; `for_document(document_id)` |
| `PGEditionRepo` | `get_by_key(edition_key)`; `upsert_key(tx, edition_key, csl=None)`; `list_keys()` |
| `PGWorkRepo` | `insert(tx, draft)`; `get(work_id)`; `get_by_slug(slug)`; `set_current_revision(tx, work_id, revision_id)`; `update(tx, work_id, *, expected_updated_at, **fields)`; `archive(tx, work_id)` |
| `PGWorkRevisionRepo` | `insert(tx, draft)`; `get(revision_id)`; `latest(work_id)`; `copy_forward(tx, revision_id) -> Revision` (copies blocks, occurrences, items, links with new ids and the same `block_key`/`citation_key`); `freeze(tx, revision_id, content_hash)`; `publish(tx, revision_id)`; `supersede(tx, revision_id)` |
| `PGWorkBlockRepo` | `upsert(tx, revision_id, draft, *, expected_updated_at) -> Block`; `tree(revision_id) -> list[Block]` ordered depth-first; `by_key(revision_id, block_key)`; `delete(tx, block_id)` (RESTRICT on children) |
| `PGCitationRepo` | `insert_occurrence(tx, draft)`; `insert_item(tx, draft)`; `for_block(block_id)`; `for_revision(revision_id)`; `by_key(revision_id, citation_key)`; `citing_span(span_id)`; `citing_key(edition_key)` |
| `PGWorkLinkRepo` | `add_source_link(tx, …)`; `add_entity_link(tx, …)`; `for_block(block_id)`; `for_span(span_id)`; `for_entity(entity_id, relation)` |
| `PGWaiverRepo` | `insert(tx, draft)`; `for_revision(revision_id) -> list[Waiver]` |

Domain models in `domain/works.py`, `domain/citations.py`, `domain/spans.py`:
pydantic v2, `X` for rows and `XDraft` for inserts, validators on drafts (for
example `CitationItemDraft` enforces `edition_id or edition_key` and
`quoted_text implies source_span_id` before the database does).

### 6.3 Services

All services take `transaction_factory` as a constructor kwarg, as
`ExtractionExecutor` does, and open one transaction per public method that
writes.

**`CitationService.attach(...)`** is the write boundary from target §11.2 and
the whole of `work_cite`. In one transaction:

1. Resolve the block by `block_key` in the work's current draft revision;
   refuse if the revision is not `draft`.
2. Resolve identity: `edition_key` → `PGEditionRepo.get_by_key`; set
   `edition_id` when found, keep `edition_key` either way. Neither given →
   `AUTH_CITATION_EDITION_MISSING`, nothing written.
3. If a `quote` is given: verify it with the window hint when the caller passed
   one (Step 2 §2.7b), else with `document_id`, else corpus-wide. `not_found`
   or `no_canonical_text` → nothing written, error returned, the attempt logged
   (structlog now; `verify_attempts` row once that table exists).
4. Narrowing rule per intent (§6.5). An error blocks the write; a warning is
   returned with the result.
5. `PGSourceSpanRepo.resolve` with the returned coordinates.
6. Insert the occurrence (`citation_key` = caller's or a new UUID; placement
   `inline`) and the item (span id, typed quote, tier, `verified_at = now()`,
   locator, identity).
7. Return the marker `{{cite:<citation_key>}}`. The caller places it in the
   block text; `work_validate` checks the bijection.

**`WorkService`**: `create`, `get`, `upsert_block`, `link`, `archive`.
`upsert_block` compares `expected_updated_at` and raises a conflict; the tool
returns `{"error": {"code": "conflict", …}}`.

**`WorkValidationService.validate(revision_id, gate)`** returns findings with
rule ids (Appendix A). Structural checks: markers ↔ occurrences bijective per
block; parents in the same revision; every item's `edition_key` known to
editions or `AUTH_EDITION_KEY_UNKNOWN`; span staleness via the Step 3 query;
`near` items without a matching waiver; region rule per intent; ungrounded
blocks of type `translation_unit` or `quotation` (warning).

**`WorkPublicationService`**: `freeze(work_id, message, waivers)` validates
with gate `freeze`, refuses on blockers, inserts waiver rows, computes the
content hash (sha256 over the ordered tuple of `(block_key, parent_key,
position, block_type, title, body_markdown)` plus sorted citation and link
rows, rendered as canonical JSON), sets `frozen_at` and state. `publish`
validates with gate `publish`, then sets state and `published_at`. Any edit to a
frozen revision goes through `WorkRevisionRepo.copy_forward` into a new draft.

**`WorkTraceService.trace(target)`** walks block → occurrences → items → spans
→ documents, and block → links → spans/entities; for a span, the reverse:
which blocks, which anchors. Output is a tree, not prose.

**`WorkExportService`** (drafting loop, decision 14): `export_draft(work_id)`
renders the current draft revision to markdown per §6.4;
`import_draft(work_id, markdown, dry_run)` parses it per Step 5 §5.2, matches
blocks by the `<!-- block:<key> -->` comments, creates a new draft revision by
`copy_forward` and applies inserts, updates, deletions and reorders, resolves
`{{cite:<key>}}` markers against existing occurrences (a marker with no
occurrence is `AUTH_CITATION_MARKER_DANGLING`, and the import refuses), and
returns the diff. `dry_run` rolls the transaction back, the pattern
`services/ingestion/structure.py` uses.

### 6.4 The exported markdown (both the draft loop and the port diff)

```
---
work: <slug>
title: "<title>"
type: <work_type>
revision: <revision_number>
state: draft
citations:
  - key: <citation_key>
    intent: quotation
    document_id: <uuid>
    char_start: 3326
    char_end: 3546
    quoted_text: "…"
    verify_status: normalized
    edition_key: TDNT_1964
    locator: {volume: II, page: 64}
---

<!-- block:<block_key> -->
## Heading

<!-- block:<block_key> -->
Paragraph text with a marker {{cite:<citation_key>}} in it.
```

Footnote definitions are not emitted; rendering to a reader's format is
`work_render` in Phase 0 and `work_export` with manifest later.

### 6.5 Validation rules that are new in this step

| Rule id | Severity | Fires when |
|---|---|---|
| `AUTH_SPAN_NOT_NARROWED` | error | intent is `quotation` or `translation` and the item's span coincides with a passage row's `[char_start, char_end]` for the current chunker, or exceeds `MAX_QUOTE_CHARS` (default 1000) |
| `AUTH_SPAN_REGION` | warning | intent is `support`, `source` or `definition` and the span coincides with a passage row |
| `AUTH_EDITION_KEY_UNKNOWN` | warning | `edition_key` has no `bibliography.editions` row |
| `AUTH_FILE_DRIFT` | warning | a flipped work's exported file differs from the last export (F2); reported by `work_validate` when the works directory still holds the file |
| `AUTH_STALE_WRITE` | error (tool-level conflict) | `expected_updated_at` does not match |

Work-type policy: a JSON document per `work_type` under
`settings.works_policy` (default built-in) mapping rule id to `error`, `warn`
or `allow`. The defaults above are the core floor for the four core work
types; packs may register stricter validators through the plugin registry the
way filter extensions are registered today.

### 6.6 Keeping `bibliography.editions` populated

At ingest, after `documents.insert`, if `metadata.edition_key` is present call
`PGEditionRepo.upsert_key(tx, key)` in the same transaction. Add the call in
`services/ingestion/orchestrator.py` next to the `document_texts.put` call.
Packs keep writing the key into document metadata as they do today; nothing
else changes for them.

### 6.7 The eight tools and two commands

Register each as a module in `mcp/tools/` with `TOOL_NAME`, `TOOL_DESCRIPTION`,
`TOOL_SCHEMA`, `handler(container, *, …)`, imported and appended to
`CORE_TOOL_MODULES` in `mcp/dispatch.py`. Schemas are in Appendix B.

| Tool | Service call | Notes |
|---|---|---|
| `work_create` | `WorkService.create` | creates the work and revision 1 as `draft`; slug must be unique |
| `work_get` | `WorkService.get` | ordered block tree with occurrences, items and links inlined; `revision` optional |
| `work_block_upsert` | `WorkService.upsert_block` | `expected_updated_at` required for updates |
| `work_cite` | `CitationService.attach` | §6.3; accepts `window` from a search hit |
| `work_link` | `WorkService.link` | source link by span id or coordinates (resolved), entity link by entity id |
| `work_validate` | `WorkValidationService.validate` | `gate`: `none`, `freeze`, `publish` |
| `work_trace` | `WorkTraceService.trace` | by slug, block key, citation key, span id or document id |
| `work_freeze` | `WorkPublicationService.freeze` | waivers passed inline as rows to insert |
| `research-engine work export --draft <slug> --out <path>` | `WorkExportService.export_draft` | Typer sub-app `cli/work.py`, added with `app.add_typer(work_app, name="work")` |
| `research-engine work import <path> --slug <slug> [--dry-run]` | `WorkExportService.import_draft` | prints the diff; non-zero exit on refusal |

Also add `work show`, `work validate`, `work freeze` as thin CLI wrappers over
the same services, following `cli/verify.py`: build the container, call the
service, `finally: await container.close()`.

### 6.8 Tests

| Test | Asserts |
|---|---|
| revision copy | `copy_forward` preserves `block_key` and `citation_key`; new row ids everywhere |
| frozen is immutable | every mutating repo method refuses a frozen revision |
| marker bijection | one marker per occurrence and one occurrence per marker, in one block; both failure modes report their rule ids |
| cross-revision parent | a `parent_id` from another revision is rejected by the composite FK |
| `work_cite` atomicity | induce a failure after the span insert; nothing from the call persists |
| identity join | `work_cite` and `claim_upsert` over the same coordinates share one `source_span_id`; a second `work_cite` creates an item and no span |
| tier per row | two citations of one span, one typed clean and one with OCR noise, keep `exact` and `normalized` respectively |
| narrowing | a `quotation` cite whose span equals a passage's bounds is refused with `AUTH_SPAN_NOT_NARROWED`; the same span with intent `background` is accepted |
| window hint | a quote copied from a hit resolves inside the window without a whole-document search (assert `find_raw` is not called) |
| editions | ingest a fixture with an edition key; the editions row exists; `work_cite` with that key sets `edition_id` |
| freeze | a `near` item without a waiver blocks; with a waiver row the freeze succeeds and the hash is stable across two computations |
| drafting loop | export a draft, edit a paragraph and add one, import as a new revision; block keys of untouched blocks are unchanged, the edited block keeps its key, the new block has a new key |
| flip drift | edit the exported file of a flipped work; `work_validate` reports `AUTH_FILE_DRIFT`; the database is unchanged |
| entity link query | target §10's "every block rendering λόγος" SQL returns the fixture block |
| stale spans | bump a fixture document's parser version; `work_validate` reports `AUTH_SOURCE_SPAN_STALE` on the citing block |

**Done when** the Lev 25 work is ported for real, its first revision freezes
with zero errors, `work_get` returns it, the drafting loop round-trips it with
a markers-only diff, and master §12 items 1 through 4 and 6 hold on it.

---

## Step 7 — On their triggers, not in this guide

| Item | Trigger | What it needs |
|---|---|---|
| `work_export` with manifest | first publication | renderer walks the tree, resolves markers, emits Markdown/HTML plus a manifest (revision hash, exporter version, validation result, waivers); CSL waits for P3 |
| `block_claim_links` (Phase B) | first work asserting a ledger claim after 010 | one table (target §9.2), `work_link` gains a claim target, `AUTH_OPEN_DEPENDENCY` becomes live |
| `bibliography.*` full (P3-1) | first academic submission | extend the stub, add `bibliography.work`, make the document join a FK, tighten `citation_identity_ck` |
| `verify_attempts` | first post-flip failed verification worth remembering | the DDL in Step 3; the log call in `CitationService.attach` and `claim_upsert` switches from structlog to a row |
| project membership | first reuse of a work in a second project | target §4.3 |
| lemma-consistency checks | first cross-work translation query | pack validator over `block_entity_links` |
| block FTS / embeddings | first measured retrieval need over authored text | target §14; never into corpus passages |
| `argument.derivations` | master §11 item 5 decided | DDL in Step 3, optional block |

---

## Appendix A — Rule id catalogue

Severity is the core default; a work-type policy (Step 6) may move a rule
between `error`, `warn` and `allow`. "P0" means `work_verify` emits it over
files; "P1" means `work_validate` emits it over rows.

| Rule id | Severity | P0 | P1 | Fires when | Source |
|---|---|---|---|---|---|
| `AUTH_ENTRY_INVALID` | error | ✓ | — | a front-matter entry fails validation | this guide |
| `AUTH_DOCUMENT_UNKNOWN` | error | ✓ | ✓ | `document_id` does not exist | this guide |
| `AUTH_SOURCE_UNCHECKABLE` | error | ✓ | ✓ | the document has no canonical text (`no_canonical_text`) | bridge honesty rule; id by this guide |
| `AUTH_QUOTE_UNVERIFIED` | error | ✓ | ✓ | tier is `near` or `not_found`; a waiver row clears `near` at freeze | target §13 |
| `AUTH_SOURCE_SPAN_STALE` | error | ✓ | ✓ | P0: verified span ≠ entry span; P1: span `parser_version` ≠ document's | target §13 |
| `AUTH_SPAN_NOT_NARROWED` | error | ✓ | ✓ | quotation/translation cites a region or exceeds `MAX_QUOTE_CHARS` | decision 6 |
| `AUTH_SPAN_REGION` | warning | ✓ | ✓ | support/source/definition cites a region | decision 6; id by this guide |
| `AUTH_CITATION_EDITION_MISSING` | warning; error at publish | ✓ | ✓ | neither `edition_key` nor `edition`/`edition_id` | target §13 |
| `AUTH_CITATION_EDITION_MISMATCH` | error | — | ✓ | span's document key ≠ item's key (key comparison until P3) | target §13 |
| `AUTH_EDITION_KEY_UNKNOWN` | warning | ✓ | ✓ | P0: the document carries no key; P1: no `bibliography.editions` row | decision 12; id by this guide |
| `AUTH_EDITION_KEY_MISMATCH` | error | ✓ | — | entry key ≠ document key | bridge §4 |
| `AUTH_CITATION_MARKER_MISSING` | warning | ✓ | error in P1 | entry/occurrence with no marker | target §13 |
| `AUTH_CITATION_MARKER_DANGLING` | error | ✓ | ✓ | marker with no entry/occurrence | target §13 |
| `AUTH_CLAIM_UNRESOLVED` | info → warning → error at publish after 010 | ✓ | ✓ | a `claims:` ref has no `argument.claims` row | bridge §6; id by this guide |
| `AUTH_STATUS_UNEARNED` | error | ✓ | — | file says `published`/`review` but the gate fails | this guide |
| `AUTH_INDEX_STALE` | warning | ✓ (011) | — | file `front_matter_sha` ≠ mirror row | bridge §4 |
| `AUTH_PARENT_REVISION_MISMATCH` | error | — | ✓ | block parent from another revision (also a FK) | target §13 |
| `AUTH_CLAIM_DANGLING` | error | — | ✓ (Phase B) | claim link to a missing claim (also a FK) | target §13 |
| `AUTH_REVISION_MUTATED` | error | — | ✓ | frozen content ≠ stored hash | target §13 |
| `AUTH_BIBLIOGRAPHY_ONLY` | warning | — | ✓ | item has identity but no span | target §13 |
| `AUTH_BLOCK_UNGROUNDED` | warning | — | ✓ | `translation_unit`/`quotation` block with no source link or citation | target §13 |
| `AUTH_OPEN_DEPENDENCY` | warning | — | ✓ (Phase B) | block asserts a claim whose status is open/researching/unresolved | target §13 |
| `AUTH_UNUSED_CITATION` | warning | — | ✓ | occurrence not visible in the rendered form | target §13 |
| `AUTH_FILE_DRIFT` | warning | — | ✓ | a flipped work's file differs from its last export (F2) | decision 2; id by this guide |
| `AUTH_STALE_WRITE` | tool error `conflict` | — | ✓ | `expected_updated_at` mismatch | target §12.2 |

Removed from the catalogue by decision 3: `AUTH_LICENSE_EXPORT`.

## Appendix B — Tool contracts

All tools: `handler(container, *, …)`; inputs are JSON Schema objects in
`TOOL_SCHEMA`; UUIDs are strings; errors use the dispatch envelope. Only the
shapes are given; descriptions belong in `TOOL_DESCRIPTION`.

**Phase 0**

```jsonc
// work_verify
{ "type": "object",
  "properties": { "path": {"type": "string"},
                  "gate": {"type": "string", "enum": ["none", "review", "publish"]} } }
// → the §2.4 output

// work_citations  (exactly one selector; the handler enforces it)
{ "type": "object",
  "properties": { "document_id": {"type": "string"}, "edition_key": {"type": "string"},
                  "claim_ref": {"type": "string"} } }
// → {"matches": [...], "source": "files" | "mirror"}

// work_render
{ "type": "object", "properties": { "path": {"type": "string"} }, "required": ["path"] }
// → {"rendered": "<markdown>", "footnotes": [{"id": "c1", "text": "...", "provisional": false, "tier": "normalized"}]}

// verify_quote (extended)
{ "type": "object",
  "properties": { "text": {"type": "string"}, "document_id": {"type": "string"},
                  "window": {"type": "object",
                             "properties": {"char_start": {"type": "integer"}, "char_end": {"type": "integer"}},
                             "required": ["char_start", "char_end"]} },
  "required": ["text"] }

// find_passages hit (extended): every hit gains
// "source": {"document_title", "edition_key", "edition", "parser_version", "has_canonical_text", "has_offsets"}
```

**Phase 1**

```jsonc
// work_create
{ "properties": { "slug": {"type": "string"}, "title": {"type": "string"},
                  "work_type": {"type": "string"}, "language": {"type": "string"},
                  "abstract": {"type": "string"} }, "required": ["slug", "title", "work_type"] }
// → {"work_id", "slug", "revision_id", "revision_number": 1, "state": "draft"}

// work_get
{ "properties": { "slug": {"type": "string"}, "work_id": {"type": "string"},
                  "revision": {"type": "integer"} } }
// → {"work": {...}, "revision": {...},
//    "blocks": [{"block_id", "block_key", "parent_key", "position", "block_type", "title",
//                "body_markdown", "attributes", "updated_at",
//                "citations": [{"citation_key", "intent", "placement",
//                               "items": [{"position", "edition_key", "edition_id", "source_span_id",
//                                          "quoted_text", "verify_status", "verified_at", "locator"}]}],
//                "links": [{"kind": "source"|"entity", "target_id", "relation", "confidence"}]}]}

// work_block_upsert
{ "properties": { "slug": {"type": "string"}, "block_key": {"type": "string"},
                  "parent_key": {"type": "string"}, "position": {"type": "integer"},
                  "block_type": {"type": "string"}, "title": {"type": "string"},
                  "body_markdown": {"type": "string"}, "attributes": {"type": "object"},
                  "expected_updated_at": {"type": "string"} },
  "required": ["slug", "position", "block_type", "body_markdown"] }
// → {"block_id", "block_key", "updated_at"}; conflict → {"error": {"code": "conflict", ...}}

// work_cite
{ "properties": { "slug": {"type": "string"}, "block_key": {"type": "string"},
                  "intent": {"type": "string", "enum": ["source","support","contrast","background","definition","translation","quotation","see_also"]},
                  "quote": {"type": "string"}, "document_id": {"type": "string"},
                  "window": {"type": "object"},
                  "edition_key": {"type": "string"}, "edition_id": {"type": "string"},
                  "locator": {"type": "object"}, "prefix": {"type": "string"}, "suffix": {"type": "string"},
                  "placement": {"type": "string", "enum": ["inline", "block_end"]},
                  "citation_key": {"type": "string"} },
  "required": ["slug", "block_key", "intent"] }
// → {"occurrence_id", "citation_key", "marker": "{{cite:<key>}}",
//    "item": {"source_span_id", "char_start", "char_end", "verify_status", "edition_id", "edition_key"},
//    "warnings": ["AUTH_SPAN_REGION"]}
// refusal → {"error": {"code": "validation_error", "message", "details": {"rule_id": "AUTH_QUOTE_UNVERIFIED", "tier": "not_found"}}}

// work_link
{ "properties": { "slug": {"type": "string"}, "block_key": {"type": "string"},
                  "relation": {"type": "string"},
                  "source_span_id": {"type": "string"},
                  "document_id": {"type": "string"}, "char_start": {"type": "integer"}, "char_end": {"type": "integer"},
                  "entity_id": {"type": "string"},
                  "confidence": {"type": "number"}, "note": {"type": "string"} },
  "required": ["slug", "block_key", "relation"] }
// exactly one target: source_span_id | (document_id, char_start, char_end) | entity_id

// work_validate
{ "properties": { "slug": {"type": "string"}, "revision": {"type": "integer"},
                  "gate": {"type": "string", "enum": ["none", "freeze", "publish"]} },
  "required": ["slug"] }
// → same shape as work_verify, keyed by block_key and citation_key instead of path and id

// work_trace
{ "properties": { "slug": {"type": "string"}, "block_key": {"type": "string"},
                  "citation_key": {"type": "string"}, "source_span_id": {"type": "string"},
                  "document_id": {"type": "string"} } }
// → a tree: {"root": {...}, "children": [...]} down to document_texts offsets, and for a span or document, up to every citing block and anchor

// work_freeze
{ "properties": { "slug": {"type": "string"}, "message": {"type": "string"},
                  "waivers": {"type": "array", "items": {"type": "object",
                              "properties": {"rule_id": {"type": "string"}, "subject": {"type": "string"}, "reason": {"type": "string"}},
                              "required": ["rule_id", "reason"]}} },
  "required": ["slug"] }
// → {"revision_id", "revision_number", "content_hash", "state": "frozen"} or {"error": {"code": "validation_error", "details": {"blockers": [...]}}}
```

## Appendix C — Fixtures

| Fixture | Where | Contents |
|---|---|---|
| `lexicon_fixture.txt` + `.json` | `tests/integration/fixtures/works/` | a short "lexicon entry" text with a curly-quoted sentence, an em dash, a line-break hyphen, two passage bounds that split a sentence, and a JSON sidecar listing every known span by name |
| `fixture_work.md` | same folder | a work file citing the fixture document: one `quotation` (exact), one `quotation` typed with straight quotes (normalized), one `background` citing a whole passage (region, allowed), one `quotation` citing a whole passage (not narrowed), one entry with a changed word (near), one with a nonexistent quote (not_found), one straddling entry, one `claims: [TEST-001]` ref |
| `Corpus` helpers | `research_engine/testing/corpus.py` | add `add_span`, `add_work` (Step 6), each tracked for cleanup; extend `CorpusFootprint` to the new schemas as they land |

Integration tests write the fixture work into a `tmp_path` works directory
and point `RE_WORKS_DIR` at it through `load_settings(works_dir=tmp_path)`.

## Appendix D — Intent, role, and granularity

| Front-matter `role` (legacy) | Default `intent` | Note |
|---|---|---|
| supports | support | |
| rebuts | contrast | |
| context | background | |
| asserts | quotation if `quoted_text` appears verbatim in the body, else source | |

`role` stays on an entry only when the citation also backs a `claims:` ref; at
the port it is kept in `work_revisions.metadata.port.front_matter` and becomes
a `block_claim_links` relation in Phase B.

| Intent | Region allowed? | Rule |
|---|---|---|
| quotation, translation | no | `AUTH_SPAN_NOT_NARROWED` error |
| support, source, definition | warned | `AUTH_SPAN_REGION` warning |
| see_also, background, contrast | yes | none |

## Appendix E — Fixed by this guide (for the researcher to review)

Items no design doc specified, fixed here so the guide is implementable. Each
is a one-line change if you disagree.

1. Setting name `works_dir` / `RE_WORKS_DIR`; files beginning with `_` are not works.
2. CLI names: `research-engine work verify | citations | render | set-key | show | validate | freeze | export --draft | import`.
3. Rule ids marked "this guide" in Appendix A, and the severities of `AUTH_CITATION_EDITION_MISSING` (warning until publish) and `AUTH_CLAIM_UNRESOLVED` (info before 010).
4. `MAX_QUOTE_CHARS = 1000`; window slack `len(quote) + 256`; "region" means exact coincidence with a passage row's bounds for the current chunker.
5. `HitSource` field names and `has_offsets`.
6. Waivers table name `authored.waivers` and its `subject` column.
7. Block-boundary heuristics (Step 5 §5.2), `citation_key = uuid5(work_id, "cN")`, the `<!-- block:<key> -->` comment, the exported markdown format (Step 6 §6.4).
8. The content hash recipe (Step 6 §6.3).
9. `verify_attempts` is a separate later migration, not part of 009.
10. `argument.derivations` omitted from 010 by default.
11. Editions backfill inside migration 012 using `gen_random_uuid()`, and the ingest hook in the orchestrator.
12. Phase 0 `work_citations` scans files; the mirror is used only when 011 exists.
13. `AUTH_STATUS_UNEARNED` for a file whose `status:` outruns its gate.

## Appendix F — Change log

Record every deviation from this guide, every contract gap found by the real
work, and every rehearsal defect here, newest first, with the date and the
step.

- 2026-09-06 — `zotero_key` renamed to `edition_key` everywhere (migration
  013): the key never touched Zotero's servers — a plain string naming an
  edition — and the name kept suggesting an account nobody needs. Columns
  (`bibliography.editions`, `authored.citation_items`, `argument.anchors`),
  the `citation_items_zotero_idx` index, the unique constraint, the
  `core.documents.metadata` JSON key (values preserved in the migration),
  domain fields, tool/CLI params (`--zotero` is now `--edition-key`),
  front-matter entries, the export format, `work_get` output, and the
  `AUTH_ZOTERO_KEY_UNKNOWN` / `AUTH_ZOTERO_KEY_MISMATCH` rule ids all move
  together. Method names (`get_by_key`, `upsert_key`) and the free-text
  `edition` field stay. No frozen work exists, so no stored hash or waiver
  names the old field. The migration round-trip test now runs against an
  isolated scratch database: the dev corpus holds real editions rows that
  must not be dropped to test a migration.
- 2026-09-05 — `work_cite` inherits its edition: when the caller names no
  edition, `attach` takes the span's document key (with its edition id when
  one exists) instead of refusing. Explicit identity still wins untouched —
  it is what the mismatch check tests the span against — and the refusal
  stays for spanless cites and keyless documents, where there is nothing to
  inherit (the check constraint requires an identity on every item row, so
  no migration). §6.3 step 2's "neither given → refuse" now reads "neither
  given nor inheritable → refuse".
- 2026-09-05 — Step 6 P1 spine built on `John-Cusack/works-phase0` with the
  researcher's sign-off (Step 5 rehearsal and the real-work port wait on the
  researcher; the Step 6 gate is waived in the same item as Step 3's). Six
  services, eight tools, five CLI commands, the ingest hook, and the §6.8
  table (20 integration tests, all passing). Choices where the guide is
  silent, and defects the tests caught in committed code:
  - `copy_forward` never ran before this step and was broken twice: the
    occurrence loop unpacked a list of ids as a list of rows (`TypeError`
    on any copy with citations), and multi-block revisions collided on
    `(revision, parent, position)` because the first pass parked every block
    parentless at its real position. The first pass now parks blocks at
    transient negative positions; the second pass restores parents and
    positions together. Fixed in place, no migration.
  - `import_draft` sets the new revision current in the same transaction;
    otherwise the imported draft is unreachable (get, validate, and freeze
    all default to current).
  - `freeze` validates with its inline waivers as *prospective*: `validate`
    takes an optional prospective set, cleared like stored rows. Validating
    before inserting made inline waivers unable to ever clear a blocker.
  - `AUTH_CLAIM_UNRESOLVED` is not emitted: rows hold no claim refs and the
    ledger has no `claim_upsert`, so there is nothing to resolve refs
    against. It goes live with Phase B claim links.
  - `AUTH_UNUSED_CITATION` fires when an occurrence's marker sits in another
    block — the occurrence is not visible in the rendered form of its own
    block. The per-block bijection stays missing/dangling only.
  - `AUTH_CITATION_EDITION_MISSING` is defensive in P1: the check constraint
    guarantees an identity on every stored item, so it fires only on rows
    written around the service.
  - Export appends a missing marker only for `block_end` occurrences (that
    placement renders at the end by definition); a missing inline marker
    stays a `MARKER_MISSING` error, not silently repaired.
  - Non-heading titles ride in a `<!-- title: … -->` comment: rows have
    titles, §5.2 markdown has nowhere to put them, and dropping them would
    lose data.
  - A heading without stored `attributes.level` renders as `##` and
    reimports without a change: the import comparison defaults a missing
    level to 2.
  - A `near` quote whose prefix will not locate is refused with
    `AUTH_QUOTE_UNVERIFIED` (no address to store), not asserted on.
  - `uuid_utils` ids never cross into pydantic: services convert to stdlib
    UUIDs at draft boundaries (`WorkBlockDraft`, occurrence/item drafts,
    import keys). Repos keep uuid7 for row ids, which the driver accepts.
  - Trace by block or citation key fans out over `PGWorkRepo.list` (new
    read); the edition-mismatch check reads through `PGEditionRepo.get`
    (new read); the freeze message writes through
    `PGWorkRevisionRepo.set_message` (new write). Protocols and the surface
    guard cover all three.
  - `Corpus.cleanup` deletes works before spans: items RESTRICT spans, so
    the old spans-first order fails teardown on any cited span.
  - The Step 5 port mapping (§5.3, uuid5 keys, `metadata.port`) is not
    implemented: with no real work to port, the rehearsal procedure waits
    on the researcher and only the draft loop it exercises is built.
- 2026-09-05 — `work_cite` built at the researcher's direction: the MCP can
  now make citations (verify, resolve, emit a paste-ready entry). Choices:
  - The tool name comes from the master's Resolver protocol, which governs
    `work_cite` — but Appendix E.2's CLI list does not include `cite`; the
    `work cite` command extends that list, flagged here for sign-off.
  - Refusals (`near` and below) store nothing and report code
    `quote_unverified` with the tier: no mirror (Step 4 skipped) and no
    `verify_attempts` (Appendix E.9) exist to record them in.
  - The window validator is duplicated between `verify_quote` and `work_cite`;
    a third consumer should hoist one shared helper.
  - `Corpus.adopt_span` mirrors `adopt` for spans a citer resolves outside
    the helper; untracked spans fail teardown loudly rather than leaking.
- 2026-09-05 — Step 3 gate waived in one item by the researcher: no real
  work verifies by hand yet (`RE_WORKS_DIR` still holds only the contract),
  so Step 3 proceeds against the fixture. Schema defects the real work would
  have caught may surface as later migrations rather than doc edits.
  Step 3 choices where the guide is silent:
  - The `passage_id` cache resolves best-overlap first, newest chunker
    (`created_at`) breaking ties; rows without offsets never match.
  - A span past the stored text's end is a `ValueError`, not a short row;
    a span on a textless document is `NotFoundError`.
  - `test_schema_truthfulness` now compares (schema, index) pairs over every
    schema `schema.py` knows, since evidence/argument indexes are not in core.
  - The migration round-trip test refuses to downgrade over data instead of
    destroying it.
  - `claim_upsert` not built: the guide specifies the ledger tools through
    the program doc, whose week has not come; Step 3 stays two migrations,
    one repository, and their tests.
- 2026-09-05 — Phase 0 built on branch `John-Cusack/works-phase0` at
  `80f5909` (Step 1.3 doc commit on top of `3b5251d`). Part 0 code map
  re-checked against live code; no discrepancies. No Appendix E item
  objected to — all implemented as written. Choices where the guide is
  silent, Step 2:
  - §2.4 check 8 (`AUTH_EDITION_KEY_UNKNOWN`) fires only when the entry
    carries a `edition_key` the document lacks. An entry with neither key
    nor edition is covered by `AUTH_CITATION_EDITION_MISSING` alone.
  - §2.4 dangling markers ignore markers matching an *invalid* entry id;
    `AUTH_ENTRY_INVALID` already reports those.
  - §2.4 tool and CLI default gate is `none`: findings are reported, nothing
    is judged, until a gate is requested.
  - §2.4 `verify_all` collects unparseable files under `unreadable` instead
    of failing the whole run; one bad file hides no work.
  - §2.5 claim-selector matches are work-level (citation fields null plus
    `claim_ref`), since `claims:` refs attach to the work, not an entry.
  - §2.5 `works_mirror_available` is a constant false in Phase 0 — no
    migration means no table to detect. Table-existence detection lands
    with Step 4.
  - §2.5 `work_citations` skips unparseable files with a warning, for the
    same reason `verify_all` does.
  - §2.6 render strips pre-existing footnote definitions before appending,
    so re-rendering never doubles them. The tier tag comes from a
    windowless verify; a metadata `date` renders as its first four digits.
  - §2.7b a `window` without `document_id` passes through to the service,
    which defines it as a whole-document search; no extra error invented.
  - Header and entry validation forbid unknown keys (`extra="forbid"`), so
    a typo'd field refuses rather than parses.
  - `front_matter_sha` is sha256 over the YAML block substring as read,
    UTF-8 encoded, fences excluded.
  - Ports gained `get_many` / `parser_versions`; pre-existing protocol gaps
    (`update_metadata` etc.) left alone.
  - The §2.2 ingest convention is documented in the in-repo
    `packages/plugins/README.md`; the out-of-tree packs (logos,
    academic-journal, kindle, yourcloudlibrary) carry their own READMEs
    outside this repo.
  - Step 1.2 real work absent: `RE_WORKS_DIR` holds only `README.md` and
    `_TEMPLATE.md`, so the Step 2 done-when is demonstrated against the
    fixture work (`tests/integration/fixtures/works/fixture_work.md`);
    the real-work `review`-gate check waits on the researcher.
- 2026-09-04 — guide written against engine `3b5251d`; no deviations yet.
