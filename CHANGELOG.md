# Changelog

## Unreleased

### marginalia-ai 0.6.3 carries a Rust extension

Every `marginalia-ai` wheel now includes `research_engine._native`, built
from the repository's Rust crates. On Python 3.13 it runs quote
normalization (6–9x faster), prose chunking (4x), markdown parsing (2.7x) and
structural chunking (1.5x). Each path returns exactly what the pure-Python
code returns. Other Python versions keep using the pure-Python code, because
the extension's Unicode tables match 3.13's. `RE_RUST_BACKEND=python` forces
the Python path, and `=rust` forces the extension.

Wheels are prebuilt for Linux x86_64 and aarch64 (glibc 2.17+), macOS on Apple
silicon and Intel, and Windows x64. Other platforms install from the source
distribution, which needs a Rust toolchain; so does `uv sync` in a checkout.
There is no API change, so plugins declaring `core_api: ">=0.6,<0.7"` still
load.

## 0.6.2 — 2026-09-19

### Backups and status are safe to operate

`research-engine backup create` now archives the complete database rather than
only `core`; fresh restores therefore include bibliography, evidence, argument,
authored, Alembic, and plugin-owned schemas. `research-engine status` masks the
database password instead of printing the raw connection URL.

A guarded maintenance script can recover DOI and JSTOR identity from stored
first-page text without re-ingestion or re-embedding. Dry-run is the default,
ISBN candidates remain report-only, and safe application refuses existing or
duplicate edition keys.

### Inference and plugin runtime failures are explicit

Core's `EmbeddingUnavailable` now derives from the SDK's public error, so
plugins can catch the documented contract without class-name compatibility
logic. Remote embedding HTTP 408, 429, 502, 503, and 504 responses stop bulk
ingestion immediately; the bundled server marks only genuine memory pressure as
safe for adaptive batch reduction.

`marginalia-ai-sdk 0.6.1` adds the core-configured database URL to
`PluginContext` as a redacted `SecretStr`. Database-backed plugin tools
therefore use the same connection as core even when `RE_DB_URL` came from
core's `.env` rather than the process environment.

## 0.6.1 — 2026-09-18

### Shared vocabulary and documents keep stable identity

Shared entity, event, and relation vocabulary keeps its first declarant and
every value that declarant defined, while later plugins may complete attributes
the owner left absent. History can therefore supply `replies_to`'s
`replied_by` inverse without a false redefinition warning. Inverse metadata is
not yet used for edge traversal.

PDF ingestion now recovers DOI, JSTOR stable URL, and validated ISBN identity
from the document's first page, stores the normalized `edition_key`, and links
the document to `bibliography.editions`. Migration `020_document_editions`
materializes already-declared edition keys and adds the explicit document FK.
File ingestion deduplicates different artifacts carrying the same stable
identity; plugin component documents may still share one edition.

### Local checkouts use the released plugin environment

The workspace now locks the published Logos, Academic Journal, and
YourCloudLibrary distributions in a default `plugins` dependency group, and
the repository MCP configuration starts `uv run research-engine serve`.
Running `uv sync` therefore cannot silently remove the plugins the checked-out
server expects.

Core now exposes `research-engine db current` and
`research-engine db upgrade` from an installed wheel. Runtime startup refuses a
database behind the packaged Alembic head with that exact remediation instead
of crashing on whichever new table is queried first. The unused
`RE_AUTO_MIGRATE` setting was removed; schema changes remain explicit.

The public README now leads with MarginaliaAI's purpose, developer-preview
status, PyPI installation, database prerequisites, and source-checkout path
rather than the state of one local corpus.

## 0.6.0 — 2026-09-17

### Distribution names are the MarginaliaAI family

Before the first public release, every distribution moved to the
`marginalia-ai` family: core publishes as `marginalia-ai`, the standalone
contract as `marginalia-ai-sdk`, and plugins as `marginalia-ai-plugin-<id>`
(the in-tree pack is `marginalia-ai-plugin-history`). Nothing had been
published under the old `research-engine-*` names, so there is no alias.

Only distribution names changed. The `research-engine` command, the
`research_engine` / `research_engine_sdk` / `history` import packages, the
`research_engine.plugins` entry-point group, plugin ids, and the
`~/.research-engine` data directory are unchanged, so configuration and MCP
setups keep working.

If you have an editable install of the old names, uninstall them
(`uv pip uninstall research-engine research-engine-sdk
research-engine-plugin-history`). Leaving both installed makes plugin
discovery report a duplicate plugin id and load neither claimant.

### Core and standalone SDK

- Replaced the conditional SDK shim with the standalone, typed
  `marginalia-ai-sdk 0.6.0` contract: manifest v2, DTOs, scoped clients,
  decorators, errors, chunking helpers, and plugin contract utilities.
- Replaced Git/copy/runtime-pip plugin installation with no-import Python
  entry-point discovery, exact artifact approval, atomic staged registration,
  explicit plugin database migrations, and the distribution lifecycle CLI.
- Added migration `019_plugin_activations`, preserving all legacy plugin
  manifests, permissions, and source provenance as disabled audit rows.
- Split local inference, normal documents, Docling, OpenAI, and the embed
  server into explicit extras. The base artifact no longer requires Torch,
  sentence-transformers, Docling, or OpenAI.

### marginalia-ai-plugin-history 0.2.0

- Packaged history as the reference plugin distribution with a schema-v2
  `history/plugin.yaml`, SDK-only runtime imports, packaged extraction schemas,
  and complete schemas for both MCP tools.
- Added wheel discovery, approval, atomic load, and standalone artifact
  contract coverage.

### The claim ledger has one atomic write path and a fidelity view

`claim_upsert` now writes a claim, its outgoing edges, and its evidence anchors
in one transaction. Every anchor is checked against canonical text before the
transaction starts; `not_found` and `no_canonical_text` refuse the whole call,
`asserts` anchors must name a person, and all writers resolve through the shared
`evidence.source_spans` identity instead of inserting coordinates directly.
The caller's typed quote and verification tier stay on the anchor while the
span keeps the canonical slice and parser version.

`anchor_context` reads the canonical text around a ledger anchor, every anchor
on a claim, a work-file citation, or a bare source span, with the quotation's
offset marked in the returned window. `work_citations(context=true)` exposes
the same view for existing work citations, batching the text slices rather than
loading whole documents. This is a fidelity check: verification proves the
characters exist; context lets a reader judge whether the source bears the use.

This implements the guide's currently ungated Steps 2–4. The audit rules,
argument graph, leverage ranking, re-verification, and edition migration remain
behind the real-ledger-row triggers the guide specifies; no placeholder methods
or tools were added for them.

### Lemma lookup that produces citable references (`find_lemma`, migrations 015-017)

`find_lemma` asks the question a lexicographic survey exists to ask — where
does this word occur — of `core.words` rather than of the text, and returns
**verse references, never character spans**. The index is built on WLC and the
survey quotes LHB (52 citations to 7); the editions agree on verse count in all
929 shared chapters but only ~47% are consonantally identical, so a WLC offset
does not address the same characters in LHB. A reference survives the hop and
`verify_quote` resolves the span in the edition being cited. Verified over all
579 occurrences of the two survey lemmas: 574 of the words are present verbatim
in the LHB verse at the same reference and all 574 verify as `exact` there. The
five that are not are exactly the five rows with `from_qere` true — WLC reads
the qere where LHB prints the ketiv — so the tool reports that flag as a
warning rather than leaving it to be discovered.

`homograph` is a first-class parameter with three states (absent, a letter, the
empty string), because 59,061 rows carry an OSHB letter splitting a number
Strong's conflated. Prefixes are counted rather than normalised away: `k/4941`
is 37 occurrences of "according to the *mishpat* of", `b/4941` is 33.

**Versification and book identity are now data** (migration 016). LHB and WLC
write `ECC HO MIC NAH` where ESV writes `EC HOS MI NA`, so a join on the
article code returned silence — not an error — for four of the thirty-nine Old
Testament books, and those four were among the versification-divergent ones.
`core.edition_books` maps `(edition, code)` to an OSIS id with a canonical
ordinal; `core.verse_map` holds the 1,978 mappings in `wlc/VerseMap.xml` that
the WLC ingest discarded; `core.editions_versification` names a scheme per
edition. The map is a pair table rather than an offset column because seven
mappings are `type="partial"` — a verse beginning midway through another — and
no integer offset expresses one. Completeness is checkable: joined on the raw
code 23 books diverge, joined through `edition_books` 27 do, and VerseMap maps
exactly 27.

Migration 015 indexes `core.words` on `(language, strong)`. A Strong's number
is unique only inside its lexicon, and closing the latent H4941/G4941 collision
is cheapest while every row is Hebrew.

Where `core.words` belongs was settled before any of this was built, while it
still had zero consumers: it stays in core as a token index, with the boundary
at "core owns tokens, a pack owns what they mean". Reasoning and the revisit
trigger are in `docs/design/decision-001-words-belongs-in-core.md`.

### `requires.plugins` is enforced

Parsed in `plugins/manifest.py` and read by nothing, so a pack depending on
another pack's chunker failed at use time with `Unknown chunker:
verse_boundary` and nothing naming the missing install. The loader now checks
it against the packs that will actually load, and repeats to a fixed point so a
dropped pack takes its dependents with it.

### The HNSW index was gone, and nothing could have noticed

Migration 006 typed `passage_embeddings.embedding` and built an HNSW index on
it. The column was still typed and the index was gone; every semantic search
had quietly returned to a parallel sequential scan (~275 ms warm on 94,858
vectors, against 1.9 ms once rebuilt). Migration 017 rebuilds it.

The invisibility mattered more than the loss. `schema.py` declared the column
as `Vector()` with no dimension and no index, and `test_schema_truthfulness`
asserted only `declared - actual` — which cannot see an index the database has
lost when the declaration never had it either. The test now also asserts
`actual - declared`, and that direction immediately found five more real
indexes nobody had written down, including the GiST index behind every ltree
subtree test and the GIN index that is the whole of keyword search. All are now
declared. Separately, `VACUUM FULL core.passage_embeddings` reclaimed 974 MB of
TOAST that had never been vacuumed.

### The completeness guard for `core.words` is now a proof

`unclaimed_letters` stripped every samekh, pe and nun from a gap before looking
for a Hebrew letter, so a dropped word spelled only from those three letters
emptied its own gap and reported nothing — 33 words of the corpus are spelled
that way. Marks are now claimed by the span each `<seg>` occupied, which no
spelling can imitate. The repaired guard passes all 929 chapters and reproduces
the same 305,517 rows, so the index was complete; the guard could not prove it.
`research-engine doctor` now asks the same question of the stored rows, so a
re-parse cannot silently invalidate 305,517 offsets.

### Quotations report the node they sit in, not just their chunk

`verify_quote` answered "where is this" with the locators of the passages
covering the match, which describe the chunker's window rather than the
quotation: a quote in Genesis 18:19 came back as "Genesis 18:18-23", and two
quotes from the same verse reported different ranges because a chunk boundary
fell between them. `QuoteLocation` gains a `node` block — the deepest node
whose span encloses the match, via `find_by_span`, which was built and never
called. `locators` stays for the page numbers it has always carried, now
documented as chunk-wide. A quotation crossing a verse boundary is enclosed by
no verse and resolves to the chapter, which is right. **This changes an
existing tool's response shape.**

### The verse boundaries of the LHB and ESV chapters, recovered

WLC was the only Bible here that knew where its own verses were, because its
chapters could be re-rendered from morphhb and checked byte for byte. LHB and
ESV have no source in this repository, so their 7,809 passages carried `{}` for
a locator and sat under no structure: a quotation could name only the 500-token
chunk it came from. Both editions lay their chapters out predictably, so the
verses were read back out of the stored text under a guard — `coverage_gaps`
blanks out every span a parser claims, and what remains must be verse markers
and whitespace and nothing else, so a parser that lost a verse gets its chapter
refused rather than written from a guess. All 929 LHB and 1,189 ESV chapters
pass, and both agree with WLC on the verse count of all 929 shared chapters.

### The WLC ingest is reproducible from the repository

The Westminster Leningrad Codex had been in the corpus since 2026-09-07 with
the code that put it there living only in `/tmp` — the OSIS extractor, the
ingest driver, and two backfills. Every editorial decision about what this
witness reads was encoded there and nowhere else. `tests/unit/test_wlc_extract.py`
now pins them: morpheme slashes are morphology; a maqqef binds and a paseq
stands apart because of the source's own spacing; setumah and petuchah stay;
the qere is the running text with the ketiv kept in `metadata.ketiv_qere`; a
ketiv spanning two words is replaced whole; ketiv velo qere takes its orphaned
maqqef with it; and `alternative` and `exegesis` notes are apparatus.

### The Hebrew text is indexed by lemma (migration 014)

A pointed Hebrew word has no single searchable form: *mishpat* is written 204
distinct ways across the WLC, and its commonest spelling finds 21 of 422
occurrences. The analysis was never missing, only discarded — every one of
morphhb's `<w>` elements carries a Strong's number and a parsing code, and
`_word_text` reads character data, so the attributes were invisible to the
ingest. `core.words` holds 305,517 of them, addressed the way passages and
nodes already are. The hard part is knowing the index is complete, and a count
cannot tell you: one row per word means any word missing from the table leaves
its letters in a stretch of text no row claims.

### `zotero_key` is now `edition_key` (migration 013)

The edition identifier never touched Zotero's servers — a plain string in
this database — and the name kept implying an account nobody needs. Every
column, field, tool and CLI parameter, front-matter key, export field, and
the two rule ids move together (`AUTH_EDITION_KEY_UNKNOWN`,
`AUTH_EDITION_KEY_MISMATCH`); values are preserved, including the
`core.documents.metadata` keys. No signup exists or ever did.

### `work_cite` inherits its edition from the cited document

Passing `edition_key` or `edition_id` on every cite was friction without
function: the span's document already names its edition. `attach` now
inherits the document's key (and edition id) when the caller names neither;
explicit identity still wins and is what the edition-mismatch check tests
against. The `AUTH_CITATION_EDITION_MISSING` refusal stays for spanless
cites and keyless documents, where there is nothing to inherit.

### Works as rows: the Phase-1 spine (create, cite, validate, trace, freeze, draft loop)

Eight MCP tools and five `research-engine work` commands draft a work as
database rows: `work_create` starts the work and its revision 1;
`work_block_upsert` writes blocks under optimistic locking (`conflict` on a
stale `expected_updated_at`); `work_cite` verifies a quote, resolves its
span, applies the narrowing rule for the intent, and writes the occurrence
plus item atomically — any refusal names its rule id and stores nothing;
`work_link` types one edge to a span or an entity; `work_validate` judges a
revision at gate `none`, `freeze`, or `publish` with Appendix A rule ids
keyed by block and citation; `work_trace` walks grounding down to document
offsets or up to every citing block; `work_freeze` validates, records waiver
rows, hashes the authored content, and seals the draft. `work export
--draft` renders the §6.4 markdown and `work import` reads an edited file
back into a new current draft revision (copy-forward; dangling markers
refuse the whole import). Ingest keeps `bibliography.editions` behind
`documents.insert`, so row citations join edition keys. Per-work-type policy
lives under `RE_WORKS_POLICY` (error, warn, allow over the core floor).

### `copy_forward` repaired for multi-block revisions

The revision copy never ran before the Phase-1 tests and was broken twice:
it unpacked a list of block ids as a list of rows (`TypeError` on any copy
with citations), and parked every copied block parentless at its real
position, colliding on `(revision_id, parent_id, position)` whenever two
blocks shared a position under different parents. The first pass now parks
blocks at transient negative positions; the second pass restores parents
and positions together. Fixed in place, no migration.

### Making citations: `work cite-entry` verifies a quote and resolves its span

`research-engine work cite-entry --document <uuid> --quote "<text>" --intent <intent>`
(tool `work_cite_entry`) is the file-phase write boundary for new citations: it verifies the
quote (exact or normalized to pass), resolves the span — creating the
`evidence.source_spans` row on a miss — and prints a paste-ready front-matter
entry carrying the *verified* offsets plus its YAML. Anything below
exact/normalized is refused with nothing stored. A `--window` pins a repeated
quote to a search hit's span; without one the first occurrence wins. The entry
id is echoed, never collision-checked — `work verify` judges narrowing and
markers afterwards.

### The span table and the claim ledger (migrations 009 and 010)

Two additive migrations, no tools yet. `evidence.source_spans` owns one row
per cited address — `(document_id, char_start, char_end)` plus the canonical
slice at it — and every span writer goes through `PGSourceSpanRepo.resolve`,
which reads the slice itself (callers pass no text) and converges concurrent
writers on one row through `ON CONFLICT DO NOTHING` plus a re-select. The
passage cache on the row is best-overlap and `SET NULL`: re-chunking may drop
it, and the overlap query rebuilds it. `argument` holds `claims` (with its
`ref` unique and its edges RESTRICT-guarded), `claim_edges`, and `anchors`,
whose address is the shared span while the typed quote and tier stay the
row's own. `verify_status` keeps only `exact | normalized | near` — enforced
by a check constraint, so `not_found` can never be stored. Staleness is a
query (`stale()`), not a column: a span is stale when its parser version
differs from its document's. Downgrading past either migration drops its
tables and schema cleanly.

### Works Phase 0: verify, cite, and render created works from files

Works live as markdown files under `RE_WORKS_DIR` until their first freeze.
Every citation is a front-matter entry naming a document and exact character
offsets, and three tools plus their CLI commands check them against the
corpus — no database migration, no new tables.

- **`research-engine work verify [path] [--gate review|publish]`** (tool
  `work_verify`) parses the file and checks each entry in a fixed order:
  entry validity, document existence, canonical text, quote tier (exact or
  normalized to pass, with the entry span passed as the verify window),
  span staleness, the region rule (a quotation or translation citing a whole
  passage, or more than 1000 characters, is `AUTH_SPAN_NOT_NARROWED`; support,
  source and definition citing one warn `AUTH_SPAN_REGION`), edition identity,
  Zotero key agreement, and body markers. Dangling markers are errors, claim
  refs report `AUTH_CLAIM_UNRESOLVED` as info until the ledger exists, and a
  file saying `published` that fails the publish gate earns
  `AUTH_STATUS_UNEARNED`. Review fails on any error; publish additionally
  fails on a missing edition.
- **`research-engine work citations (--document | --zotero | --claim)`**
  (tool `work_citations`) lists the works citing a source by scanning the
  files. Exactly one selector.
- **`research-engine work render <path> [--out <file>]`** (tool
  `work_render`) appends one footnote definition per entry, in id order, with
  author, title and year from document metadata and the verified tier. Any
  missing part renders as `document <id>` and tags the note `[provisional]`.
- **`research-engine work set-key <document_id> <ZOTERO_KEY>`** stores a
  Zotero key in a document's metadata for the later join. Packs that know
  their material's key should write `metadata["zotero_key"]` at ingest;
  nothing enforces it, and its absence is a `work_verify` finding.

### Search hits carry their citation draft, and verify takes a window

Both change an existing tool's response.

- **`find_passages` hits gain a `source` block**: the document title, the
  Zotero key and edition when a pack wrote them, the parser version, and
  whether the hit has canonical text and offsets at all. A hit without
  offsets or text is not a citation draft. Read batched — one document query
  and one text query per result page, never one per hit.
- **`verify_quote` gains an optional `window`**: `{char_start, char_end}`
  where the quotation is believed to sit, e.g. the span from a search hit.
  The window is checked first (exact, then folded); on a miss the
  whole-document search runs unchanged. `QuoteVerifier.verify` takes the same
  `window` keyword.

### A document demoted out of structural chunking says so

Three built-in modules shipped for months returning `"structural"` from
`default_chunker()` and handing the pipeline no section table. `run_chunking`
falls back to prose windows, which produce perfectly good passages — so nothing
looked wrong, and the structure was simply gone until someone went looking. The
fix for each parser is merged; this makes the next one announce itself.

The hard part is not logging it, it is not crying wolf. A markdown file with no
`#`, or an HTML page with no `<h1>`, genuinely has no structure and prose
windows are the right answer. What separates that from a defect is that a parser
which *counted* headings and then supplied none has dropped them:

- **`structural_sections_missing`** at `warning` — `heading_count`,
  `section_count`, `chapter_count` or `div_count` is non-zero while the section
  table is empty. The structure was found and lost.
- **`structural_sections_absent`** at `info` — nothing was counted either, so
  the document really is flat.

Both name the parser, which `run_chunking` now takes as `parser_id` from the
orchestrator; without it the log could say a document was demoted but not what
demoted it.

`reindex chunks` needed no change: it already refuses structural documents
outright rather than re-chunking them without their table.

### HTML and TEI get the section table their chunker always needed

Both modules returned `"structural"` from `default_chunker()` and then handed
the pipeline no `metadata["sections"]`. `run_chunking` reads a missing table as
"this format has no structure" rather than "this parser forgot to say", so both
silently fell back to prose windows — right for plain text, wrong for two
formats built out of headings. Nothing about the resulting passages looked
wrong, which is why it survived: this is the same defect fixed in `markdown.py`,
found by looking for it rather than by anything failing.

**HTML.** Headings become sections with their tag number as the level, and
markup inside a heading is flattened for the label (`First <em>Nested</em>
Section` reads as one line). The canonical text is *unchanged* — the walk
reproduces `get_text(separator="\n", strip=True)` exactly, asserted directly,
so no offset moves and nothing needs re-ingesting. Only exact `NavigableString`
counts: `Comment` and `Doctype` subclass it, and an `isinstance` check would
have pulled comments into the document as prose.

**TEI**, where reading the output closely turned up two further defects:

- **Nested divs stored their prose twice.** `.//div` matches at every depth and
  `itertext()` already descends, so a two-chapter part held each chapter once
  under itself and once under the part.
- **Adjacent blocks were welded together.** `"".join(el.itertext())` turned
  `<head>Chapter A</head><p>Alpha body.</p>` into `Chapter AAlpha body.` — a
  word on neither side of the seam, embedded and searched as one.

Divs are now walked in document order, each section holding its div's own prose
and stopping at its first nested div, with nesting depth as the level. Sections
are disjoint and `build_node_tree` widens parents over their children, the same
contract `sections_from_markdown` keeps.

`TEIXMLModule.version` goes to `2.0` because its canonical text moves. No
document in the corpus uses either parser, so there is nothing to re-ingest —
this was a trap set for the first HTML or TEI document, not a live fault.

### The author a PDF names is kept, when it names one

Three of the four Docling documents in the corpus carry an author in their
metadata — `McClellan, George B. (George Brinton), 1826-1885` and the like —
and it was read by nothing. EPUB has stored `metadata["author"]` since it was
written; this brings the PDF path level with it.

`/Author` is junkier than `/Title`: of the 22 PDFs here that fill it, 15 name
something that is not a person. Every rule in `_usable_author` rejects one of
them — a login welded from an initial and a surname (`PWinter`, `SBenigno`), a
software licensee (`Registered to: GEICO`), the scanning app (`CamScanner`), an
authoring default (`Administrator`, `(anonymous)`), or an internal identifier
(`PP53454`, `Pagination_Cover`).

The login rule is the one with a real edge: `^[A-Z]{2}[a-z]+$` catches `PWinter`
while leaving `McClellan` and `MacArthur` alone, because a genuine name of that
shape has a lowercase second character.

What survives is stored verbatim, including the compound fields a library scan
carries — `McClellan, George Brinton, 1826-1885; Prime, William Cowper,
1825-1905. Life, services, and character of...` is one string holding two people
and a subtitle. Splitting that into separate contributors is a different
problem, and guessing at it would destroy the record of what the file claims.

`_pdf_metadata_title` and `_pdf_metadata_author` now share `_declared`, so the
gate and the file access stay separate concerns.

As with the title fix: no re-ingest and no version bump. The three authors were
derived by the new function and merged into the existing `documents.metadata`,
leaving `sections` and `pages` untouched.

### A PDF's title comes from the PDF, not from its first line of OCR

`_extract_title` took the first non-blank line of converted text. On a scanned
book that is whatever the layout model read first, which produced these four
titles in the corpus:

| stored | actually |
|---|---|
| `fies` | The campaigns of napoleon |
| `Mfo mm` | McClellan's own story |
| `CENTRAL` | The Civil War papers of George B. McClellan |
| `Graduate Theses, Dissertations, and Problem Reports` | Metaphors of Reading |

A library stamp, a torn word, an OCR fragment, and a repository's boilerplate
header. All four PDFs stated their correct title in metadata that nothing read.

Metadata cannot simply be trusted either: of 88 PDFs here, 54 declare a title
and 19 of those are junk. So `_usable_title` gates it, and every rule rejects
something real from that sample — an authoring default (`Document1`,
`(anonymous)`), an account or part number (`1099`, `749537 NCM9JP01`), a
filename the tool copied into the field (`Microsoft Word - PFS Editable.doc`,
`B87023352[1].pdf`), or an export identifier
(`output_CSantiago_fmlrKYMGCeW3Nj3`, recognised by an underscore that is not
followed by a space, which real titles do not have).

A trailing extension is stripped rather than rejected. `Rape Gang Inquiry
Report.docx` is a real title wearing one, while `B87023352[1].pdf` still fails
the letter rules once it is off — rejecting on the extension alone lost the
first to save nothing.

The first-line heuristic stays as the second step, because a PDF assembled from
scans often declares nothing at all.

No re-ingest and no version bump: canonical text, offsets, passages and
embeddings are all untouched by this, and the title is one column. The four
stored titles were re-derived by the new function and written directly, along
with the root `document_nodes` title that breadcrumbs actually display.

### Markdown headings survive ingestion

`MarkdownModule` treated `#` as formatting and stripped it alongside the
emphasis and link syntax. Headings are not formatting — they are the only
structure the document has, and the canonical text is what every later pass
reads. A heading deleted on the way in is therefore gone for good: no reindex
can recover it, because reindexing re-reads the stored text and finds the same
absence. The one markdown document in the corpus was stored with all 39 of its
headings removed, 50,740 characters on disk arriving as 49,949.

The same module also declared `structural` as its chunker and then handed the
pipeline no section table. The pipeline reads a missing table as "this format
has no structure" rather than "this parser forgot to say", so it fell back to
prose windows — correct behaviour for plain text, silently wrong here. It now
emits `metadata["sections"]` from `sections_from_markdown`, the boundaries-only
table EPUB already supplies.

Docling leaves `#` lines in the markdown it exports, so with this a markdown
file and a converted PDF finally reach storage in the same shape.

`heading_count` now counts the section table rather than the raw file, which
makes the two agree: a `#` line inside a fenced code block is stripped before
headings are read, and is not a heading.

`MarkdownModule.version` goes to `2.0`. Documents parsed at `1.0` need
re-ingesting rather than reindexing. The corpus's one such document was
re-ingested: 1 node became 40 across four levels, its 30 prose windows became 46
sections, and every passage now points at a node.

### Chapters recovered from plain-text books

Sixteen books arrived as flat text from library and e-reader exports — no
markdown, no tags — so each had a structure layer of exactly one node covering
the whole book. Window expansion had nothing to bound against, and a breadcrumb
could say no more than the title.

The chapter headings were in the prose all along. Finding `Chapter N` is easy;
the difficulty is that a contents list, a back-of-book index, a notes section and
a reference list all contain it and look identical to a chapter start one line at
a time. Every rule in `sections_from_chapters` exists to separate a chapter
*start* from a chapter *mention*, and each was added because it was measured to
be necessary on a real book here:

- **Ascent.** A contents list and the chapters it lists are both numbered from
  one, so they form two runs rather than one confused sequence.
- **Median gap ≥ 5,000 characters.** This is the rule that does the real work.
  Chapters have a book between them — measured, 24k–105k characters — while a
  contents list, an index and a notes section sit 30–3,400 apart. Without it the
  detector chose a contents list for one book and a 27-entry back index spanning
  0.5% of the text for another.
- **Runs that continue each other are rejoined.** A list naming chapters 4–8
  sits between chapter 8 and chapter 9 in one book, breaking the ascent so it
  read as two shorter books.
- **The sequence must reach 55% of the way through.** A section runs to the next
  heading or to the end of the text, so a run that gives up at 8 of 27 would
  title chapter 8 over the remaining 63% of the book. Better no structure than
  wrong structure.

Six of the sixteen yield a sequence — 68 chapters across them — and ten
correctly yield none: four are activity books with numbered steps rather than
chapters, one has its only run in a notes section 84% of the way through, and one
breaks at 8 of 27 and is refused.

This runs through `reindex structure`, so nothing is re-parsed, re-chunked or
re-embedded: only `document_nodes` and the `node_id` of existing passages change.
Markdown headings still win where a document has them — chapter detection is
inference over prose and must never override an author's own structure.

**Found on the way, not fixed here:** `modules/markdown.py` strips `#` markers
from canonical text at ingest, so one document stores 49,949 characters where its
source file has 50,740, and its 39 real headings are gone from the text for good.
No reindex can recover them; it needs a re-ingest once the markdown module stops
destroying them.

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
