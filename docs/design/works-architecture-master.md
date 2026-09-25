# Works and citations — master architecture

**Status:** Controlling doc. Written 2026-09-04 against `main` @ `3b5251d`
(migrations 001–008; migration 009 and P3 **not** built).
**Supersedes** the conflicting sections of its two companions — both remain in
the repo as detail specs; where they disagree, this doc decides:

- `authored-works-architecture.md` — the **target model** (authored layer, blocks,
  revisions, links, export). Its §20 rejections describe why the *end state* is
  not file-based; they do not govern the interim.
- `work-citations-architecture.md` — the **bridge spec** (Phase 0: files, verify,
  index tools, identity bridge). Its authority model is interim by design.
- **Revises** `research-program-on-marginalia.md` §2 (shared span primitive
  replaces inline anchor spans) and §6/§10 (created works enter the engine as
  first-class objects at the flip trigger — a deliberate, stated departure from
  "outputs are files outside," ratified by this doc).
- **Amended 2026-09-04** by the researcher's fourteen decisions, logged with
  their options in `works-architecture-diagrams.md` and prompted by the audit in
  `works-architecture-master-review.md`. Sections touched are listed in §13.
- **Implemented by** `works-implementation-guide.md` (2026-09-04): one chapter
  per build-order step, against the engine at `3b5251d`.

---

## 0. The one-paragraph decision

Created works are **first-class, revisioned research objects in the engine** —
DB-canonical at the target, with the authoring surface staged: markdown files
under `works/` are canonical **until a work's first freeze**; after that the
`authored.*` tables are canonical and files become exports. One shared span
primitive (`evidence.source_spans`) is the single copy of source-side anchoring
across claims, annotations, and works; each citing row keeps its own typed quote
and verification tier (decision 1). Citations are rows with FKs from day one,
identified by `edition_key` through a minimal `bibliography.editions` stub until
P3 fills it out (decision 12). Nothing in the target schema depends on P3, and
nothing in the bridge is thrown away: the file contract was designed as a subset
of the target rows.

The invariant both companions share, restated as the master's own:

> **Every publishable assertion or translation decision descends from the
> authored block, through a typed citation or link, to a named edition and —
> when the source is in the corpus — to the exact characters that support it.**

---

## 1. Resolved conflicts

The two companions disagree on four points. Each is resolved by a call plus the
trigger that moves the system from the bridge position to the target position:

| # | Conflict | Bridge (work-citations) | Target (authored-works) | **Call** |
|---|---|---|---|---|
| 1 | Authority | Files canonical; DB is a rebuildable mirror | DB canonical; files are exports | **Staged.** Files canonical until first freeze; `authored.*` canonical after. Both models are honest; only the flip is a decision (§3) |
| 2 | Address inside the work | None — flat markdown + `[^cN]` markers | `block_key` across revisions; offsets rejected for authored prose | **Target, when Phase 1 starts.** §6.4 of the target doc is correct: char offsets are the wrong address for mutable prose. The bridge needs no in-work address; that is why it is cheap |
| 3 | Span normalization | Address inline in each table | `evidence.source_spans`; everything FKs to `source_span_id` | **Target, immediately — this one is not staged.** It revises the program doc's 009 schema before it is ever built, so there is no migration to undo (§4) |
| 4 | Sequencing | Tools in a day; migration on pull | Phase A commits the subsystem up front | **Bridge first, target trimmed.** Phase A is reduced to an eight-tool spine plus a drafting loop (§8); everything else waits on its trigger (§9) |

Conflict 3 is decided now because it is free: migration 009 exists only as a
design. Deciding it after `010_argument` lands would cost a migration and a
re-anchor; deciding it now costs a doc edit.

---

## 2. What does not change

Carried unchanged from both companions and the program doc — restated so the
master is self-contained:

- **Source-side address:** `(document_id, char_start, char_end)` into
  `document_texts.text`. Never `passage_id`; `passage_id` is a `SET NULL` cache
  repointed on re-chunk.
- **Verify tiers are load-bearing:** `exact` / `normalized` / `near` /
  `not_found`, and the engine's fifth answer `no_canonical_text`, which is never
  stored. `normalized` is never collapsed into `exact`; `near` blocks freeze
  unless waived with actor, reason and timestamp (§7); `not_found` never enters
  `source_spans` or a Phase-1 citing row — the Phase-0 mirror records it
  honestly and the gate refuses it; "document has no canonical text" is a
  different answer from "quote not found."
- **`parser_version` drift makes spans stale**, visibly — never silently
  re-anchored.
- **Deletion protection:** anything a claim, annotation, or citation rests on is
  `ON DELETE RESTRICT` into `core.documents`.
- **`core.edges` is not the citation graph** — polymorphic `(kind, id)` endpoints
  have no FK integrity; citations and links are typed tables.
- **Rendered citation strings are never authoritative.** Free text is permitted
  at exactly one boundary — machine-generated output — and nowhere as input.
- **Works are not `core.documents`.** Drafts must never be searchable as corpus
  evidence; authored search is scoped and labeled separately.

---

## 3. The staged authority model

### Phase 0 — files canonical (now)

`works/*.md` front-matter is the authority for what a work cites. The database
holds a derived, rebuildable mirror (`core.works_index`, `core.work_citations` —
bridge doc §4). Four tools operate on it (§8). Identity is bridged by
`edition_key` (bridge doc §5); claim refs are inert text until 009. This phase
needs **no migration** to start and answers "which works cite this source" from
day one.

The files live in a **configured works directory outside the engine repo**;
`work_path` everywhere is relative to that root (decision 7). Every front-matter
citation carries a required `intent` from the target's vocabulary (quotation,
translation, support, contrast, background, definition, source, see_also);
`role` is optional and present only when the citation also backs a claim ref
(decision 11; `works/README.md`).

### The flip trigger: first freeze

A work flips to DB-canonical when its **first revision is frozen** — the moment
immutability, diffs, and publication gates start to matter more than editing
friction. Before that, files are cheaper and diff via git; after, the target
model is the point. Concretely, a work is ported by:

1. `work_create` + one `work_block_upsert` per block (block boundaries from the
   markdown structure);
2. one `work_cite` per front-matter citation entry — the entry fields map 1:1
   onto occurrence + item rows (that was the design constraint); `intent` maps
   directly, and an entry with neither `edition_key` nor `edition` fails the §5
   CHECK, which is why `work_verify` flags zero-identity entries from day one;
3. one `work_link` per claim ref / entity mention worth typing.

The flip, as a protocol (decision 2, review §4.2):

- **F1 — Per-work flip, global tooling.** A work flips at its first freeze;
  migration 012 and the Phase-1 tools land once.
- **F2 — Post-flip, the file is a read-only export.** `work export` rewrites it
  and git diff is the audit trail. A manual edit to a flipped work's file is
  drift: the next validate run reports it; the database never follows the file.
  (Scope: the flip applies to pre-012 file works — today only
  `works/mishpat-tsedaqah-survey.md`; works created after 012 are DB-born
  per F4.)
- **F3 — `work_index` and the mirror are one unit.** They live while any
  pre-012 work is unflipped and drop together when the last one flips. Their
  rows are derivable from files, so the cost is zero.
- **F4 — New works after 012 are DB-born** (`work_create`). The file-first path
  survives only as *import* (target §15.2), explicit and transactional.
- **F5 — The port is one transaction per work**: `work_create` + block upserts
  + `work_cite` + `work_link`, then export regenerates the file. The only
  acceptable diff is citation markers (`[^cN]` → `{{cite:<key>}}`); anything
  else is a port bug, and the port logs the marker mapping.
- **F6 — Flip rehearsal before the first real freeze.** Port one work as a
  drill, export, diff, discard. The flip decision is re-taken, not assumed, at
  more than ten works or the first cross-work lemma question (review §3.4).

`works/README.md` remains the file contract and becomes the bundle-import
format (target doc §15.2) — one markdown dialect, two authorities at different
lifecycle stages.

### Phase 1 — DB canonical (target)

`authored.works` → `work_revisions` → `work_blocks` with stable `block_key`;
freeze computes a content hash and locks content + links; publish is a state
change on a frozen revision; editing a frozen revision copies forward into a
child revision. Adopted from target doc §6. The
`UNIQUE NULLS NOT DISTINCT` constraint on block positions requires **Postgres
15**; the dev server is `pgvector/pgvector:pg15` (review §1.3), so no fallback
is needed.

**Drafting after the flip (decision 14).** Prose-heavy editing goes through an
explicit round trip, not sync: `work export --draft` writes the current draft
revision as markdown, the author edits it, and `work import` reads it back as a
*new* draft revision with a dry-run diff. Citation markers survive because they
live in block text. Block-by-block editing through `work_block_upsert` remains
the agent's path. Whether block-level markdown with markers is enough (target
§21.1) is what the F6 rehearsal tests.

---

## 4. `evidence.source_spans` — the one copy of anchoring (revises program doc §2)

The shared primitive, adopted from target doc §7 and revised by decision 1
(Option B: the span owns coordinates, the canonical slice and parser provenance;
each citing row owns its typed quote, tier and locator):

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
    -- Span identity: one row per (document, coordinates). Same offsets = same
    -- slice of the same document; there is nothing else to distinguish. The
    -- unique index also serves range lookups, so no separate index is needed.
    CONSTRAINT source_spans_coordinates_uk UNIQUE (document_id, char_start, char_end)
);

-- Decision 4: the only durable record of a failed verification after the flip.
-- Built at first need; until then the gap is this comment.
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

**Resolver protocol** (normative; applies to `work_cite` and `claim_upsert`):

1. Before insert, look up `(document_id, char_start, char_end)`; reuse the id on
   a hit. Insert only on a miss, in the same transaction (`ON CONFLICT DO
   NOTHING` + re-select for the race).
2. `quoted_text` on the span is the canonical slice, not the typed quote. The
   typed quote, its tier and its `verified_at` are properties of the citing row.
3. `locator` is citing-row data. Two citers of one span may cite different
   pages or verses.
4. Re-verify *discovers* in one table (`parser_version` mismatch against
   `document_texts`), then recomputes each citing row's tier from its own typed
   quote.
5. Target §7's `normalized_quote_hash` is dropped: it existed for span-level
   tier recomputation, which now happens on citing rows.

Why this is decided immediately rather than staged: the three consumers were
about to build three copies of the same columns plus the same verify provenance,
and every future maintenance tool (`reverify`, drift reports) would have to
visit all three. One row per verified span means:

- **claim ↔ work joins become identity joins.** A claim anchor and a work
  citation over the same span share `source_span_id` — "this essay renders the
  evidence of that claim" is a FK match, not the span-overlap geometry the
  bridge doc had to settle for. True by construction only because of the
  uniqueness rule and the resolver above.
- **`reverify` discovers in one table.** A parser bump marks the affected spans
  stale once; every dependent row then re-verifies its own quote.

**Amendments this forces, decided here:**

- **Program doc §2 (`argument.anchors`) is revised:** the anchor keeps
  `claim_id`, `role`, `person_entity_id`, `edition`, `edition_key`, `locator`
  **and its own** `quoted_text`, `verify_status`, `verified_at`,
  `parser_version`, and carries `source_span_id NOT NULL REFERENCES
  evidence.source_spans(id) ON DELETE RESTRICT` instead of inline
  `document_id`/`char_start`/`char_end`. The anchoring *rule* is unchanged — it
  just now lives in one table.
- **Guide II P4-1 (`core.annotations`) is revised** the same way: annotation
  rows reference `source_span_id`; the "delete the annotation, not the source"
  protection comes from the span's RESTRICT.
- **Failed verifications never enter `source_spans`** or any Phase-1 citing
  row: a span that does not exist is not evidence. In Phase 0 the mirror records
  the failure (`verify_status` including `not_found`, bridge §4) and the gate
  refuses it; after the flip `evidence.verify_attempts` is the record
  (decision 4).

Migration numbering in §9 reflects this: the span table precedes the ledger.

---

## 5. Citations: target shape, bridged identity

Adopted from target doc §8 — occurrence (where and why a citation appears) split
from items (what is cited) — with one amendment that removes its P3 dependency:

```sql
-- Decision 12: the bibliographic stub that lands with 012. P3-1 extends it
-- (identifiers, contributors, work_id → bibliography.work) and turns the
-- document join into a FK. Until then core.documents joins to editions by
-- metadata->>'edition_key', and the edition-mismatch check compares keys.
CREATE SCHEMA bibliography;

CREATE TABLE bibliography.editions (
    id          uuid PRIMARY KEY,
    edition_key  text NOT NULL UNIQUE,
    csl         jsonb NOT NULL DEFAULT '{}',   -- CSL-JSON as imported from Zotero
    created_at  timestamptz NOT NULL DEFAULT now()
);
-- Populated at ingest: one row per distinct documents.metadata->>'edition_key'.

CREATE TABLE authored.citation_items (
    occurrence_id   uuid NOT NULL
                    REFERENCES authored.citation_occurrences(id) ON DELETE CASCADE,
    position        integer NOT NULL,
    -- AMENDED: nullable until P3 backfills it. The target doc's NOT NULL made
    -- every citation unrepresentable before edition records exist.
    edition_id      uuid REFERENCES bibliography.editions(id) ON DELETE RESTRICT,
    -- The bridge, from the work-citations doc §5: identity by edition key now;
    -- edition_id backfilled and the CHECK tightened when P3-1 lands.
    edition_key      text,
    source_span_id  uuid REFERENCES evidence.source_spans(id) ON DELETE RESTRICT,
    -- Decision 1 (Option B): the typed quote and its tier live here, per citer.
    quoted_text     text,
    verify_status   text,            -- NULL | exact | normalized | near (never not_found)
    verified_at     timestamptz,
    locator         jsonb NOT NULL DEFAULT '{}',
    prefix          text,
    suffix          text,
    suppress_author boolean NOT NULL DEFAULT false,
    PRIMARY KEY (occurrence_id, position),
    CONSTRAINT citation_identity_ck
      CHECK (edition_id IS NOT NULL OR edition_key IS NOT NULL),
    CONSTRAINT citation_verify_ck
      CHECK (verify_status IS NULL OR verify_status IN ('exact','normalized','near')),
    CONSTRAINT citation_quote_needs_span_ck
      CHECK (quoted_text IS NULL OR source_span_id IS NOT NULL)
);
```

Invariants carried unchanged from target doc §8.3: a span's document must
represent the cited edition (checked by `edition_key` comparison until P3
provides the FK); markers are
bijective with occurrences; quotation citations freeze only on `exact`/
`normalized`; bibliography-only citations (no corpus span) are legitimate and
reported as less strongly grounded (`AUTH_BIBLIOGRAPHY_ONLY`).

**Granularity by intent (decision 6).** `citation_occurrences.intent` uses the
target §8.2 vocabulary. `quotation` and `translation` must narrow to the exact
words (`AUTH_SPAN_NOT_NARROWED`, an error); `see_also`, `background` and
`contrast` may cite a region, including a whole search-hit chunk; `support`,
`source` and `definition` warn when the stored span is a whole chunk. These are
core defaults; a work-type policy may move any intent between error, warn and
allow, and packs may add stricter validators.

**Bibliographic naming:** the target doc's `bibliography.work` /
`bibliography.edition` FRBR split is adopted **and resolves the naming collision**
flagged in guide II P3-1 (whose Option B `works` table would have collided with
created works): P3-1's tables land as `bibliography.*`, never `core.works`.
The `editions` stub above is the first of them.

---

## 6. Links: source, claim, entity

Adopted unchanged from target doc §9, with phase assignments:

- **`authored.block_source_links`** (quotes / paraphrases / **translates** /
  summarizes / discusses / supports / contrasts / defines) — Phase 1. This is
  the translation-work relation: a translation block `translates` the Greek span
  and `renders` the lemma.
- **`authored.block_claim_links`** (asserts / explains / depends_on / rebuts /
  concedes / qualifies) — Phase B, blocked on 009 (`FK argument.claims`).
  Expression relations only; the claim graph stays in `argument.claim_edges`.
- **`authored.block_entity_links`** (renders / discusses a lemma entity) —
  **012** (decision 9): its FK target `core.entities` exists, and "every block
  rendering λόγος, across works and revisions" (target doc §10's query) is the
  reason the flip exists, so it ships with the spine rather than waiting for
  Phase B. The single highest-value addition for the Greek-translation program.

The "do not turn every paragraph into a claim automatically" rule (target
§4.4) is master doctrine: block links are explicit, many-to-many, and human
committed — agents propose, the author commits, the gate enforces.

---

## 7. Validation and gates

**Rule IDs and waivers-as-rows** are adopted wholesale from target doc §13 —
stable IDs (`AUTH_QUOTE_UNVERIFIED`, `AUTH_SOURCE_SPAN_STALE`,
`AUTH_CITATION_EDITION_MISMATCH`, `AUTH_OPEN_DEPENDENCY`,
`AUTH_SPAN_NOT_NARROWED`, …), warnings waivable with actor + reason + timestamp, publication policy per
work type deciding which warnings block. The bridge phase implements the subset
that operates on files (`work_verify` returns the same rule IDs from day one, so
nothing downstream depends on which phase produced the verdict).

**The precise tier rule** (review §4.6): `not_found` never enters the tables;
`near` blocks freeze unless waived, and a waiver is a row with rule ID, revision,
actor, reason and timestamp — never a flag in metadata. `work_verify` also
reports, from day one: zero-identity citations (no `edition_key`, no `edition`),
unknown `intent` values, and a `edition_key` no ingested document carries
(decisions 11 and 12).

**Rights are out of scope** (decision 3). The corpus is the researcher's private
library; this design carries no rights classification and no export gate, and
target doc §16 is not adopted. `quoted_text` on the span is the canonical slice,
kept for verification.

---

## 8. Tool surface — two tiers, one MCP container

**Phase 0 (bridge, no migration needed):**

| Tool | Job | Superseded by |
|---|---|---|
| `work_verify [path]` | verify every front-matter citation; report rule IDs; unresolved claim refs, zero-identity entries, unknown intents and unknown edition keys are findings, not errors | `work_validate` |
| `work_index` | rebuild the mirror; drift via `front_matter_sha` | — (dropped with the mirror when the last pre-012 work flips, F3) |
| `work_citations --document/--zotero/--claim` | the leverage query, works-flavoured | `work_trace` |
| `work_render [path]` | the only free-text boundary: footnotes from rows, provisional refs labeled as such | `work_export` |

**Phase 1 (the spine, decisions 5 and 8):** `work_create`, `work_block_upsert`
(optimistic concurrency via `expected_updated_at`), `work_cite` (resolves
identity, verifies the quote, resolves the span, writes occurrence + item +
marker in one transaction — the atomic write boundary from target doc §11.2),
`work_link`, `work_validate`, `work_trace`, `work_get` (ordered blocks plus
links, the read path), `work_freeze` (its trigger is the flip trigger). Plus the
drafting loop `work export --draft` / `work import` (decision 14). Deferred to
their triggers: `work_export` with manifest (first publication), project
membership (first reuse), lemma-consistency checks (first cross-work
translation query), block FTS/embeddings (first measured retrieval need —
target doc §18's own discipline).

**The citation draft (decision 6).** A `find_passages` hit is a citation draft,
not a citation: its chunk offsets are a valid address into canonical text, but
not the words a sentence rests on. Search stays a pure read. Each hit gains a
`source` block — document title, `edition_key`, `edition`, `parser_version`,
`has_canonical_text` — so the draft carries identity without a second lookup.
`work_cite` accepts the hit's chunk window as a narrowing hint: the verify
service locates the typed quote inside that window first (the from-offset hint
in `CanonicalIndex.find`), exact by construction when it is a true substring,
and falls back to the whole-document search only on a miss. `verify_quote` as
it stands remains the path for quotes that did not come from a hit. Intent
decides whether narrowing is required (§5).

---

## 9. Migration sequence (replaces both companions' numbering)

| # | Name | Phase | Notes |
|---|---|---|---|
| 009 | `evidence_source_spans` | lands with 010, program week 2, after Phase 0 | one copy of anchoring + `UNIQUE (document_id, char_start, char_end)`; `verify_attempts` at first need |
| 010 | `argument` (ledger) | program doc week 2 | anchors revised per §4: `source_span_id` replaces inline coordinates; typed quote and tier stay on the anchor |
| 011 | `work_citations` bridge | **only if** the >3-works / >20-citations trigger fires before Phase 1 starts | explicitly temporary cache of files; dropped with `work_index` when the last pre-012 work flips (F3) |
| 012 | `authored` + `bibliography` stub | Phase 1 (first freeze pending; F6 rehearsal first) | works / revisions / blocks / occurrences / items / source + entity links / waivers; `bibliography.editions` stub (decision 12); `citation_items` carries the `edition_key` bridge CHECK and its own tier columns; claim links wait for Phase B |
| — | `bibliography.*` full | P3-1 as designed, S3 first | extends the stub (identifiers, contributors, `bibliography.work`), makes the document join a FK, backfills `edition_id`, tightens the CHECK |

All additive; each revertible independently (guide I ground rule 3). The
program doc's twelve-week plan is otherwise untouched — §5's warning stands:
P3 design must survive the S3 spike before `bibliography.*` is populated, which
is why the bridge exists.

---

## 10. Superseded material, indexed

So no companion doc is silently contradicted:

- Program doc §6 "works are files, not rows" → **revised** by this doc §3 (staged
  authority; files remain the Phase-0 authority and the bundle format).
- Program doc §2 `argument.anchors` inline spans → **revised** by §4 here.
- Guide II P4-1 inline annotation spans → **revised** by §4 here.
- Guide II P3-1 Option B `works` table → **renamed** `bibliography.work/edition`.
- Bridge doc §3 (files canonical) → **interim**, until first freeze.
- Bridge doc §4 mirror schema → **interim**, lands only as 011, dropped at flip.
- Bridge doc §2 "one anchoring rule" → **kept**, now enforced by one table.
- Target doc §18 Phase A → **trimmed** to the §8 list; §4.2 repo policy →
  **closed** by decision 7 (§11); §20.1/§20.5 rejections → **accepted for the
  target**, not the bridge.
- Target doc §7 span DDL → **revised** by §4 (Option B: tier and locator on
  citing rows; `normalized_quote_hash` dropped).
- Target doc §16 rights → **not adopted** (decision 3).
- Target doc §12.1 nine tools → **the eight-tool spine plus drafting loop** (§8).
- Bridge doc §2/§4/§8 numbering (009 ledger, 010 mirror) → **renumbered** per
  §9 (009 spans, 010 ledger, 011 mirror).
- Bridge doc appendix test #6 (overlap join) → **replaced** by the identity-join
  test (review §6.2).
- Bridge doc §4 mirror `verify_status` including `not_found` → **kept** for
  Phase 0: the mirror records failures; spans and Phase-1 rows exclude them.
- `works/README.md` `role` vocabulary → **supplemented** by a required `intent`
  (decision 11); README's "files, not rows" → **restated** as "files until
  first freeze."

---

## 11. Open decisions — owned by the researcher

1. **Where Phase-0 files live** — **closed** (decision 7): a configured works
   directory outside the engine repo; `work_path` is relative to that root. The
   config key's name is an implementation detail.
2. **Flip confirmation** — **closed as a recurring decision**: the trigger is
   first freeze, re-taken at more than ten works or the first cross-work lemma
   question (F6). Any work needing lemma queries or revision history earlier
   pulls Phase 1 forward.
3. **Locator vocabularies** (verse / lexicon entry / page ranges) — target doc
   §21.3; answered by the first real translation work, per both companions.
4. **Work-type policy values** for intent granularity (§5) — set by the first
   real work of each type, starting from the core defaults.
5. **`argument.derivations` with 010.** Program doc §10 recommends it land with
   the ledger because justifications cannot be retrofitted; not decided here.

---

## 12. Definition of done

The staged system is working when, on a real work (the Lev 25 / *deror*
translation is the candidate):

1. Every citation is a row or a front-matter entry that verifies
   `exact`/`normalized`, or `near` under a waiver row — and `work_verify` proves
   it, returning rule IDs.
2. "Which works cite this source / rest on this claim / render this lemma" are
   queries, not greps — the third arrives with 012 (decision 9) and is the
   flip's justification.
3. A published sentence descends: block → citation → edition → characters.
4. Re-chunking a source breaks nothing; re-parsing it visibly stales every
   affected span, in one table.
5. A frozen revision exports deterministically with an auditable manifest.
6. Nothing that failed verification can reach `published` without a waiver row
   that says who and why.

At that point the division of labour from the program doc §1 survives intact —
the engine holds what is addressable: claims, evidence, and now the works that
speak from them.

---

## 13. Change log

**2026-09-04 — edit pass applying the researcher's fourteen decisions** (options
and reasoning in `works-architecture-diagrams.md`, "Decisions taken"; the audit
that prompted them is `works-architecture-master-review.md`).

| Decision | Sections touched |
|---|---|
| 1 Option B: span owns coordinates + canonical slice; citing rows own typed quote, tier, locator; `UNIQUE` coordinates; resolver protocol | §0, §4, §5, §9, §10 |
| 2 Flip protocol F1–F6 with rehearsal and re-take trigger | §3, §8, §9, §11 |
| 3 Rights out of scope | §2, §4, §7, §10, §12 |
| 4 `verify_attempts` at first need | §4, §9 |
| 5, 8 Eight-tool spine; `work_export` with manifest deferred | §1, §8 |
| 6 Citation draft: `source` block on hits, window-hint narrowing, granularity by intent, `AUTH_SPAN_NOT_NARROWED` | §5, §7, §8 |
| 7 Configured works directory; `work_path` relative | §3, §10, §11 |
| 9 `block_entity_links` ships with 012 | §6, §9, §12 |
| 10 009 lands with 010 after Phase 0 | §9 |
| 11 `intent` required in the file contract; `role` optional | §3, §7, §10 |
| 12 `bibliography.editions` stub in 012 | §0, §5, §9 |
| 13 Residue: bridge numbering, README opening, fifth tier, §11.1 closed | §2, §10, §11 |
| 14 Export-edit-import drafting loop | §3, §8, §10 |

Also closed: the Postgres 15 note in §3 (dev server confirmed pg15).
