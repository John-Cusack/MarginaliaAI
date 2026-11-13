# Kindle re-OCR with measured, marked transcription accuracy

**Status:** draft r3 (2026-11-11). No code exists yet. Phase 0 (measurement)
gates everything after it. Supersedes the Kindle pack's EasyOCR full-page path.
r2 folded in an external review (OMP, gpt-6.1-sol) and the operator's decision
that per-page human review is out of scope; r3 folds in the second review,
the table design, the audit decisions, and the content-filter finding.
**Related:** [`drm-pdf-ocr-ingestion-architecture.md`](drm-pdf-ocr-ingestion-architecture.md)
(the ACSM pack is the second consumer of the OCR package introduced here).

## 1. Goal and accuracy contract

Re-scrape and re-ingest every Kindle book in the corpus with no per-page human
review (~15–20k screens).

Fallible readers cannot guarantee zero errors, and neither agreement nor a
finite sample can certify it. The contract:

1. **Every stored character is confirmed or marked.** Confirmed: all three
   readers agree at character level and every independent check passes.
   Anything else is stored with an *uncertain* mark in a core-owned,
   document-level assessment that quote verification, search and citation
   tools surface.
2. **Missing content is never silent.** Ink that no reader turned into text
   blocks the page (a *gap*), it is not dropped.
3. **The residual error rate is measured.** The class marks cannot catch —
   every reader wrong the same way — is measured pipeline-wide (synthetic
   pages + calibration screens) and per book (automatic audit).
4. **Release gate (per book):** the 95% upper bound on the fraction of pages
   containing any unmarked error is ≤ 0.10 (see §9 for the exact rule and
   why it is stated per page rather than per word).

Old Kindle documents are replaced and re-embedded; existing citations are
re-anchored where that is provably correct, otherwise the old document is kept
as *superseded*.

## 2. Why: what is wrong today (measured 2026-11-11)

The Kryptonite reader renders each page as a PNG `<img>` rasterized from a
`/renderer/render` tar holding glyph IDs and vector outlines, no characters.
The search index is AES-encrypted. Decrypting it or mapping glyphs to
characters is DRM circumvention and is out of bounds; OCR of the rendered page
is the only path.

Production crops (0, 60, 1280, 840), runs EasyOCR `readtext(paragraph=True)`
on the full two-column page via the remote CUDA service, and sorts paragraph
blocks by y.

**What the measurements are.** Two pages were hand-transcribed (one prose,
one notes): per-column Tesseract psm 6 scored 0.1% CER on both; production's
full-page EasyOCR scrambled column order. The 21-page and 39-screen
comparisons score engines **against each other** (Tesseract as reference,
lowercased, punctuation stripped): they measure agreement and order, not
truth. The Docling runs used Docling's Tesseract CLI backend, so their
agreement with Tesseract is not evidence from two independent recognizers.

| Agreement measure (21 pages) | Production | Tesseract psm 6 per gutter column |
|---|---|---|
| Words also found by the other engine | ~95% | (reference) |
| Words in the same order | ~70% (13/21 pages lose >10%) | (reference) |
| "Back to NNN" popup leak | 20/21 pages | 0 |
| Note-number backsteps | 8/53 | 0/52 |
| Cost | GPU | ~1.8 s CPU/page |

Other findings:

- The pixel-gutter column split (widest ink-free band in 30–70% of width)
  agreed with `layout_data` column rects on every matched page. Matching
  `layout_data` to a screen via the scrubber was fragile in the old harness.
- Docling layout is good on dense pages; it scrambles sparse pages (TOCs,
  link lists) and labels a TOC as a table.
- Tables (e.g. *Rise and Fall*, B004774792) are small embedded rasters,
  unreadable at 1×. At 3× DPR, TableFormer recovers the grid with ~6/78
  numbers wrong; Tesseract ~10/78.
- Note links are pure-blue boxes; re-reading each alone, upscaled, with a
  `0123456789[]` whitelist and psm 7: bracketed refs 8/8 at 1×; superscripts
  need 2× (8/8 at 2×, ~half wrong at 1×).
- 2× body text ≈ 1× for Tesseract. Blanket underline removal is net-neutral.
- `page_data.wordsInPage` vs OCR token count ranges 1.1–2.6×: not a
  completeness check.
- `toc.json` is the full hierarchical TOC (`label`, `tocPositionId`,
  `entries`). Page labels can be roman or `Location N of M`; `Page N` can
  repeat across a turn, so progress uses the scrubber value.
- Amazon serves "Website Temporarily Unavailable" after rapid scrubber jumps
  across books. ~9 s/turn, 15–20 s between jumps, ~6 min between books, one
  book at a time, avoided it in every run since.
- The scraper calls a last-page detector (`kindle/scraper/reader.py:220-223`,
  checks `:460-493`) that never fires on Kryptonite; every book ends on "20
  pages with no new text". One stall truncated *Provoked* at 1827/2316.
- **Claude cannot be a full-transcription reader of book pages:** Anthropic's
  output filter blocks verbatim reproduction of long copyrighted passages
  (two audit agents were terminated on 2026-11-11). Any Claude role must emit
  differences and short snippets only.

## 3. Fixed decisions

| Concern | Decision |
|---|---|
| DRM boundary | Never decrypt the search index. Render tars are parsed **in memory** against an allowlist (`page_data_*`, `layout_data_*`, `toc.json`, `location_map.json`, `metadata.json`, `manifest.json`); `glyphs.json`, `tokens_*` and unknown members are never read or written. Raw tars are never persisted. Unknown schema → fail closed. |
| Page images | Transient: held on the super server while a book is in progress, deleted on finalize, hard cap 14 days. Never on the laptop disk, never in logs, never in backups. |
| External models | Only the audit sample (§9) may be sent to Claude, and Claude only emits differences with ≤ 8-word snippets. No other image leaves the operator's machines. |
| Human effort | No per-page review. One-time operator check of ~15 calibration screens (~1–2 h). Short exception lists only (e.g. unplaceable citations). |
| Canonical text | Three independent readers; automatic tie-break; unresolved text marked. No reader overrides another. |
| Structure | Chapters/sections from `toc.json`; page labels from the footer; tables as typed nodes. OCR headings advisory. |
| Hardware | Laptop: capture and ingest. Super server (5950X + RTX 3090): image store, reading, audit, assembly. EPYC: no role. |
| Capture pacing | One book at a time, sequentially, for every capture. |
| Audit screens | Drawn from the same screenshots captured for ingest. No separate audit capture. |
| Corpus inventory | The 14 `kindle_book` documents in the corpus DB (port 5435): B003NX6Z3W, B0090NUP8K, B0055PGVVW, B00DY3FFMS, B0117S8JHE, B00E5UJAG6, B00DB3FUTY, B00E977P7G, B08KXMN1BS, B07566KR46, B08T7RD14R, B004774792, B00457X7VI, B0DNB8ZX3L. B07D8G8XBQ pending operator decision. |

## 4. Architecture

```
laptop                                   super server (Tailscale)
┌───────────────────────────┐           ┌──────────────────────────────────────────┐
│ kindle pack: capture       │  PNG +    │ marginalia-ocr service                    │
│  Playwright → Kryptonite   │  bound    │  job store (SQLite) ─ page store          │
│  scrubber-driven turns     │  metadata │  ├ region inventory + column split        │
│  allowlisted tar parsing   ├──────────►│  ├ readers: Tesseract | local VLM | 3rd   │
│  binding + tiling checks   │  commit   │  ├ specialists: note links, tables        │
│  throttle detect / resume  │  ack      │  ├ align → confirm → tie-break → marks    │
│  targeted re-capture       │◄──────────┤  ├ assembly (+ mark propagation)          │
└───────────────────────────┘           │  ├ audit (sample of the same screens)     │
┌───────────────────────────┐           │  └ finalize → delete images               │
│ kindle pack: ingest        │  bundle   │ sweeper (systemd timer, independent)      │
│  pull final bundle         │◄──────────┤ GPU lease (host-local)                    │
│  core replace_document     │           │ embed server (existing, on-demand)        │
└───────────────────────────┘           └──────────────────────────────────────────┘
```

### 4.1 Package `marginalia-ai-ocr` (`packages/ocr` in this repo)

Workspace member; not a plugin, no entry point.

- **client** (light: `httpx`, `Pillow`): v2 wire types, job create, page
  upload, status, re-capture requests, bundle pull.
- **server** extras: Tesseract binding, Docling layout + TableFormer, the VLM
  runtime, a third recognizer, FastAPI. Runs as a user systemd unit with
  on-demand start and idle exit, like `marginalia-embed`.

Wire protocol v2: job-oriented, one page per upload, structured JSON results,
pixel cap sized for 3× (v1 rejects > 4,000,000 px at `kindle/ocr_server.py:25`;
a 3× 1280×780 crop is ~9.0 M px). Strict bounded PNG validation stays. The
verified path has **no** local or empty-output fallback (v1 falls back at
`kindle/scraper/ocr_remote.py:73-89`).

v1 is not retired unilaterally: it keeps serving until the ACSM pack moves to
v2 or ships on v1. The ACSM design doc's OCR decision is updated in the same
change that lands v2.

### 4.2 GPU lease

A host-local lease (flock-based, on the super server) with a declared VRAM
budget per holder. The OCR service, the audit stage and the embed server each
acquire it before loading models and release it on unload/idle exit. Holders
fail closed when the lease is held or free VRAM after acquiring is below
budget (the operator's TTS workloads are outside the lease, hence the
free-VRAM check). A one-time free-memory check is not exclusion; the lease is.

## 5. Capture (Kindle pack, rewrite of the `scraper/reader.py` loop)

- **Resolution:** 3× DPR for every screen if Phase 0 shows acceptable turn
  time and browser memory; otherwise 2× with targeted 3× *re-capture* (new
  pixels, never upscaling) of table/figure screens. Crops come from the actual
  image dimensions (today's crop at `reader.py:257-266` assumes 1×).
- **Response ↔ image binding.** For each settled screen, the render response
  that produced it is identified by content, not timing: the permitted
  `layout_data` textline rectangles, scaled to the screenshot, must overlay
  ink rows (per-line overlap ≥ threshold set in Phase 0), and
  `page_data` start/end positions must agree with the scrubber value and
  footer label. Two consecutive settled reads must agree. Otherwise discard
  and re-take. Phase 0 exercises stale, prefetched and delayed responses.
- **Identity:** each screen gets an immutable `capture_id` = hash(book, render
  config, DPR, position range, image hash).
- **Completeness by tiling.** Screens tile the position space from the first
  to the last position (authority: `location_map` / scrubber max). Overlaps
  are allowed and recorded with their extent; a gap triggers re-capture at
  that position. Identical image hashes at different positions are allowed
  only for blank/near-blank screens.
- **Durable progress:** a screen is done only after the server acknowledges a
  committed write. Resume starts from the server's last committed position.
- Throttle page detection, exponential backoff, slow pacing, one book at a
  time. Throttle, blank and error renders are classified and never OCR'd.

## 6. Page store, job store, retention

- One owner-only directory per job on the super server; images never logged;
  derived crops in memory only; directory excluded from backups/snapshots.
- **Retention by an independent systemd timer** (not the idle-exiting OCR
  process), plus reconciliation at every service start. Past the cap, images
  are deleted; affected unfinished pages are re-captured with a new
  `capture_id`, and results bound to the old `capture_id` are discarded.
- **SQLite job store:** jobs, captures, reader outputs, alignments, checks,
  marks, gaps, audit results, state transitions, pipeline version.
- **Bundle** (kept permanently, derived only): final text; per-region
  lines/words with boxes; each reader's reading; alignments; checks; marks
  and gaps with reasons; transformation log; table grids; `capture_id`s;
  pipeline version; audit sample, results and bound. Replaces
  `extracted/{asin}.txt`.

## 7. Reading

### 7.1 Region inventory

Every connected ink region on a screen is assigned to exactly one of: text
column, spanning heading, table, figure, note link, chrome (masked
popup/footer). Unassigned ink is a gap. Single-column pages and spanning
headings are detected, not forced into two columns. Docling layout labels are
hints; the pixel gutter splits columns. Region order is recorded and checked
against the reading order of the page (columns left→right, spanning headings
in place).

### 7.2 Readers

Each reads every text region independently:

1. Tesseract psm 6 (CPU pool on the 5950X).
2. A local vision-language model on the 3090, chosen in Phase 0.
3. A third recognizer **with its own recognition model** (EasyOCR per region,
   or PaddleOCR — not Docling-over-Tesseract), chosen by how rarely its errors
   coincide with the other two.

An empty or failed reading never counts as agreement.

### 7.3 Specialists

- **Note links:** blue-box detection, digit/bracket-only re-read.
- **Tables:** see §8.
- **Figures:** text inside figures is in scope, read by all readers, stored as
  figure text; marked unless all agree.

### 7.4 Align, confirm, tie-break, mark

- Readings are aligned **in both directions** at character level after a fixed
  normalization (Unicode NFC, ligatures expanded, whitespace collapsed). Case,
  punctuation, signs, decimals and accents are significant. Insertions,
  deletions and substitutions are all disagreements; a word present in some
  readings and absent in others is a disagreement, not a non-event.
- **Confirmed** only when all three agree and every check passes. No 2-of-3.
- Otherwise automatic tie-break, in order: (1) re-read the region from 3×
  pixels (re-capture if the screen is 2×); (2) prefer a candidate that equals
  a *confirmed* occurrence elsewhere in the same book; (3) the VLM picks among
  the readers' candidates (it cannot propose new text). The result is stored
  **marked**, with candidates and reason, permanently. Tie-break outputs never
  seed the book's confirmed vocabulary.
- Independent checks (a failure marks the affected span, or gaps the page):
  pixel line count per region = text line count; note numbers in sequence
  (resets allowed at `toc.json` boundaries); non-dictionary words absent from
  the confirmed vocabulary; consistent number format in a table column;
  headings match `toc.json` labels; chrome leaks ("Back to", throttle page,
  blank render).

## 8. Tables

No new ingest method. Representation:

- **Canonical text:** caption immediately followed by a Markdown pipe table,
  the same form Docling-parsed PDFs already store, so search and
  `verify_quote` work unchanged.
- **Node:** a `NodeDraft` with `node_type="table"` (core's `node_type` is free
  text, `schema.py:142`) under its chapter, with metadata: caption, table
  number, page label, `rows`, `cols`, the cell grid, per-cell mark flags.
- **Passage:** each table is its own passage with its caption; never mixed
  with prose; a large table splits only between row groups, repeating the
  header row in metadata (not in text, since passage text must equal its
  canonical span).
- **Reading:** TableFormer grid, cross-checked against row/column counts from
  ruling lines or whitespace; every cell (numbers, labels, signs, units) read
  by all readers and confirmed or marked like any text.
- **Continuations:** a table continuing on the next screen is detected by
  matching column headers/column count and joined into one table.

Out of scope here: core's `prose_window` chunker has no table awareness, so
existing Docling PDFs can split tables. Separate core change.

## 9. Audit and release gate

### 9.1 Why the gate is per page

The second review showed (a) extending a sample after looking at it inflates
false release (30→80 rule: 7.9% at the limit, not 5%), and (b) OCR errors
cluster on pages, so a per-word Poisson bound from a small page sample is not
valid. The gate is therefore stated on **pages**: the fraction of pages with
≥ 1 unmarked error. Errors per bad page are reported from Phase 0.

### 9.2 Audit reader

An `AuditReader` interface with implementations:

- `LocalVlmTranscriber` — the largest local VLM that fits under the lease,
  full transcription of the sampled screens at 3×.
- `ClaudeDiffChecker` — Claude sees the screen image and the final text for
  that screen, region by region, and returns only differences (location,
  final text snippet ≤ 8 words, its reading, confidence). Never a full
  transcription (content filter, §2). Anchoring bias is measured by seeded
  errors (§9.3).
- `Combined` — both; a difference from either counts.

Which one ships is operator decision D1; the gate below works with any of them
because it uses the measured sensitivity.

### 9.3 Audit sensitivity

The audit reader's sensitivity *s* to the errors that matter — unmarked errors
the production pipeline would make — is measured in Phase 0 with **seeded
errors**: on synthetic pages and calibration screens, plant real-word
substitutions, omissions, insertions, punctuation and digit changes into the
"final text" and count what the auditor reports. `s_L` is the one-sided 95%
lower bound on *s* (Clopper–Pearson). The seeded set is predeclared and
separate from the data used to choose models and thresholds.

`s` is measured **with the adjudication step of §9.4 in the loop**, because that
step is what the per-book rule applies: a difference the adjudicator does not
confirm is not a detection, and a sensitivity measured without adjudication
would not be the sensitivity the gate rests on. It is measured on two
populations, predeclared and kept apart:

1. the **planted set** — errors inserted into the final text, one reader's
   mistake or none at all; and
2. the **pipeline-shaped set** — the errors the readers themselves make, i.e.
   the places two or more of the three readers agree on a reading the page does
   not support. On crisp synthetic prose this set is empty, and an empty set
   measures nothing; the **degraded-ink** corpus variant (small caps, light ink,
   tight kerning, mild blur, with the canonical text left untouched) is what
   gives it members.

An auditor that fails `s_L ≥ 0.5` on the pipeline-shaped set does not meet the
gate, however well it does on planted errors: the errors a three-reader confirm
step lets through are exactly the ones this set contains.

The population the bound is taken over is the agreed errors that also survive the
**independent checks** the design already plans (WP2): a span a dictionary or
book-vocabulary check would mark is marked in production, so it is not an
unmarked error and does not belong in `s_L`. Until WP2 exists, a book-vocabulary
check stands in for it and is named in every report. Two classes are reported
separately rather than folded in:

- **case-only differences** (every reader answering the same word in the other
  case) are their own class and are kept out of `s_L`; the case policy is an open
  operator decision (2026-11-12), and this document's gate is not changed by it;
- **errors the checks catch**, with their count, so the size of the population an
  auditor is actually judged on is visible.

Table screens are audited cell by cell (`--audit cells`): the candidate is the
cell grid as labelled plain cell text, not the Markdown no reader produces, and
the auditor reports differences per cell. That mode has its own `s_L`, measured
on the synthetic table pages.

### 9.4 Per-book rule (predeclared, no extensions)

- Sample **n = 60 screens** from the book's own captures, before reading the
  audit results: stratified by layout class (prose, notes, table, sparse/TOC,
  chapter opener, figure) with at least 5 per class when the class has ≥ 5
  screens, the rest proportional; weights recorded.
- A **detection** is an auditor difference on an unmarked character span that
  passes both steps below: the automatic re-read, then adjudication.
  - **Re-read.** All three readers at 3× still produce the final text *and* the
    auditor still differs → the difference survives; if the re-read changes the
    result → also a detection, since the original was wrong.
  - **Adjudication** (decided 2026-11-12, D1(b)). One call per screen carries
    every span the auditor reported on that screen. The two readings of a span
    — the final text's and the auditor's own — are shown in random order, and
    the adjudicator is never told which pass produced which and never sees the
    first pass's answer. It answers, per span, which reading the image supports,
    or "unsure". Only a span the adjudicator sides with the auditor on is a
    detection; **"unsure", a skipped verdict and a failed call are all
    non-detections**. Adjudication is Claude-only: the two-stage shape of r2
    collapses to Claude, because every span here is one all three readers agreed
    on — the reader VLM included — and a local adjudicator from the VLM's own
    model family would be the same family judging itself. A local adjudicator
    from a *different* family, with its own measured `s_L`, remains open. The
    step is measured, like everything else: `s` in §9.3 is measured with it in
    the loop.
- Everything the auditor reports is marked regardless of the outcome, and a
  non-detection is not evidence that a span is right — only that it was not
  confirmed by this step. Marking is unconditional; detection is not.
- **Release** iff detections D = 0 and `3 / (n · s_L) ≤ 0.10`, i.e. `s_L ≥ 0.5`
  at n = 60. Under zero detections this bounds the fraction of pages with an
  unmarked error at ≤ 10% (95%).
- If D > 0: block; root-cause; fix; reprocess; draw a **fresh** predeclared
  sample; at most 2 retries, then the book stays blocked. Each attempt is
  recorded in the bundle.
- If Phase 0 cannot establish `s_L ≥ 0.5`, books are not released (n may be
  raised, never adapted after looking).

### 9.5 Finalize state machine

`captured → read → checked → assembled → audited(pass) → bundle_written
(hashed) → images_deleted → final`. Each step is recorded; a crash resumes from
the last recorded step; `final` is never set before deletion succeeds. A
pipeline-version change invalidates every non-final book's reads, audits and
bundles.

## 10. Assembly

- Position order; join across regions, columns and pages; strip running
  heads/footers; attach `toc.json` chapters/sections via `tocPositionId`;
  attach page labels; tables per §8.
- Every transformation is logged with its source mapping (each output span →
  `capture_id` + box; each deleted source span → reason):
  - **De-hyphenation:** join when the joined form is confirmed elsewhere in
    the book and the hyphenated form is not; URLs never joined by rule; any
    other join keeps the hyphen or, if joined, is **marked**.
  - **Running heads/footers:** removed only when position *and* content
    repeat across pages; any other removal is a failure.
  - Marks propagate through every transformation.
- Assembly checks: the multiset of confirmed source tokens equals the final
  tokens plus logged deletions minus logged joins; order is checked by
  alignment of the final text against the region sequence; no unmapped
  output span.

## 11. Core changes

### 11.1 Transcription assessment (document-level, authoritative)

New tables (migration after `020_document_editions`):

- `core.text_assessments(id, document_id, text_digest, assessor, version,
  complete bool, created_at)` — one current assessment per document; bound to
  the canonical text digest; `complete=false` means "not assessed", distinct
  from "assessed, zero marks".
- `core.uncertain_spans(assessment_id, char_start, char_end, kind
  [uncertain|gap|tie_break|figure|table_cell], candidates jsonb, reason)` —
  half-open canonical character coordinates.

Document-level storage survives rechunking (`services/ingestion/reindex.py`
rebuilds passages from canonical text without old metadata). Passage metadata
may carry a derived projection only.

Surfacing (structured fields, not only warnings):
- `QuoteVerification` gains `transcription: {assessed, overlaps[]}` computed
  against the resolved raw quote interval; `verified` keeps meaning textual
  match; MCP `verify_quote` serializes the new field.
- `find_passages` / `get_passage_context` report overlaps for every returned
  interval, including expanded windows.
- Work attach/verify (`services/works/attach.py`, `services/works/verify.py`)
  and claim creation (`services/argument/claims.py`) forward the overlap as a
  warning/finding without changing citation tiers.
- Quality-only update: an assessment can be replaced for unchanged text
  (today's ingest returns early on content hash, `orchestrator.py:210-234`).

### 11.2 Supersession

`core.documents.superseded_by uuid null` (migration). Default keyword, vector,
similarity retrieval and corpus-wide quote discovery exclude superseded
documents; explicit `document_id` access still works with a warning.

### 11.3 `replace_document`

One public operation (core service + CLI command + SDK method), one database
transaction, advisory lock on the old document id, idempotent via
`core.document_replacements(old_id, new_id, bundle_digest, state)`:

1. Ingest the new document (requires `ingest_drafts` internals that accept an
   outer transaction; `orchestrator.py:248-301` owns its own today).
2. For each `evidence.source_spans` row on the old document: find the
   corresponding occurrence in the new text using the quoted text **plus**
   surrounding context (±200 chars of old text) and page locator; enumerate
   all candidates; accept only a unique context-consistent match; map
   normalized matches back to raw offsets and verify the whole slice.
3. Re-verify every citer (claim anchors, work citation items, block source
   links) at the destination with its own full quote, no global fallback.
4. Coalesce destination collisions (`source_spans_coordinates_uk`) and rewire
   anchors, citation items and block source links atomically.
5. Spans referenced by **frozen** work revisions are never moved (moving
   would silently change grounding under an unchanged hash).
6. Re-point `ingestion_items.document_id`; extraction/mention rows on old
   passages are dropped with them unless the old document is kept.
7. If every span moved and none is frozen-referenced: delete the old
   document. Otherwise set `superseded_by` and report unplaced spans.

Today there are 10 spans on Kindle documents, all on *Just Politics*
(B0090NUP8K).

### 11.4 Ingest from the pack

`IngestionClient.ingest_drafts` with **`full_text`** (core then verifies every
passage against its span), `node_drafts` (chapters, sections, tables), and
per-passage page locators — the pattern of `logos/tools/ingest_book.py:1281-1327`
— wrapped by `replace_document` when an old document exists. The assessment
(§11.1) is written in the same transaction.

### 11.5 Pack cache and identity cutover

One identity: ASIN → job → bundle → document. `scrape_book`, `ingest_book`,
`check_book`, `list_books` move together from `extracted/{asin}.txt` to
bundles. An old `.txt` never satisfies the verified path or short-circuits
re-OCR (today `ingest_book.py:77-99` returns the existing document by source
before considering `rescrape`).

## 12. Testing without retaining licensed images

- **Synthetic pages (CI and Phase 0):** rendered from known text in the
  corpus's layouts — two columns, spanning headings, superscript and bracketed
  notes, tables with labels/units/signs and continuations, URLs, TOCs,
  figures with text, chapter openers, sparse pages. Exact character truth.
- **Seeded-error sets** for audit sensitivity (§9.3).
- **Calibration set:** ~15 real screens incl. 2 tables, captured one book at a
  time from the ingest capture of the pilot books; transcriptions drafted by a
  local model and checked once by the operator. Held out from all tuning.
  Retention: transient by default (operator decision D3).
- **Failure-injection tests** (nonlicensed): adaptive-bound counterexample;
  clustered errors; omitted words; marks surviving rechunk and expanded
  windows; quality-only update; wrong-unique re-anchor (the `form`/`from`
  swap); normalized-match collisions; frozen citations; concurrent/retried
  replacement; interrupted upload; expiry mid-book; deletion failure during
  finalize; stale/prefetched render response.

## 13. Phases and gates

0. **Measure (no product code).** Rebuild the capture harness with the tar
   allowlist and in-memory images. On synthetic + calibration: unmarked error
   rate by class (substitution, omission, insertion, order, punctuation,
   assembly) and errors per bad page; mark and gap rate per page; VLM choice
   (VRAM with TTS present, s/page); third recognizer by error correlation;
   audit sensitivity `s_L` per `AuditReader`; 3× capture cost; binding and
   tiling on complete sequential walks of 3 books (incl. a roman-numeral and
   a Location book). **Gate:** zero unmarked errors on calibration; synthetic
   page-level unmarked-error bound ≤ 0.10; `s_L ≥ 0.5` for the chosen
   auditor; processing ≤ 9 s/page.
1. `packages/ocr` skeleton: job/page store, sweeper timer, v2 wire, GPU lease,
   Tesseract + region inventory, synthetic page generator in CI.
2. Readers, alignment, confirm/tie-break/marks, checks, specialists, tables.
3. Capture rewrite in the Kindle pack.
4. Assembly, audit, finalize; core assessment tables, surfacing, supersession,
   `replace_document`; pack cache cutover; ingest.
5. Pilot: *Rise and Fall* (B004774792, tables) and *Provoked* (B0DNB8ZX3L,
   notes).
6. Remaining books, one at a time.

Capture dominates wall time: ~15–20k screens at ~9 s ≈ 40–50 h.

## 14. Risks

| Risk | Mitigation |
|---|---|
| All readers wrong the same way | Measured; per-page release gate with measured auditor sensitivity |
| VLM invents plausible text | Picks only among candidates; always marked |
| Content cut before any reader sees it | Region inventory; unassigned ink = gap |
| Assembly damages correct text | Logged transformations, mark propagation, token reconciliation |
| Screenshot bound to wrong response | Content-based binding via layout lines; tiling; stale-response tests |
| Amazon throttling or ban | One book at a time, slow pacing, backoff, resume |
| Image retention leaks | Independent sweeper, startup reconcile, no backups, crash-safe finalize |
| Old citations silently wrong | Context-anchored matching, per-citer re-verify, frozen spans untouched, supersession |
| Claude refuses page text | Diff-only role; local transcriber as alternative |
| GPU contention | Host-local lease with budgets; fail closed |

## 15. Open decisions

- **D1** Audit reader: `ClaudeDiffChecker`, `LocalVlmTranscriber`, or
  `Combined`.
- **D2** Whether B07D8G8XBQ joins the inventory.
- **D3** Whether calibration images may be kept locally outside the repo
  (default: transient, re-captured per test).
