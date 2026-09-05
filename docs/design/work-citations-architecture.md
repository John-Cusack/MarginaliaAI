# Works and structured citations — the citation spine

**Status:** Suggested / RFC. Written 2026-09-04 against `main` @ `3b5251d`
(migrations 001–008; migration 009 and P3 **not** built).
**Companion to:** `research-program-on-marginalia.md` §2 (the anchoring rule),
§6 (works are files), §10 (descent); `research-workflow-gaps.md` §3 (bibliographic
identity); `research-workflow-implementation-2.md` P3 (schema, population, tiers).
**Authoring contract:** `works/README.md`. This doc is the design behind it.

---

## 0. The problem

A citation answers three questions:

1. **Identity** — *which* source? Book, edition, translator, page.
2. **Location** — *where in it?* Not "page 214" but the exact characters the
   statement rests on.
3. **Role** — *what does it do here?* Asserts, supports, rebuts, context.

Free text concatenates all three into one untyped string. The result loses all
three at once: identity cannot be resolved to an edition, location cannot be
re-checked against the source, role exists only in the reader's head. A citation
that cannot be verified is prose wearing a footnote's clothes.

The repo's existing strength makes this worse in a specific way: the corpus is
*already* span-addressable (`document_texts`, real `char_start`/`char_end` since
P1, `verify_quote` returning the span it matched), so an unstructured citation
thrown away exactly the address the engine was built to hold.

**Goal:** every citation in a created work is a structured object —
`(document_id, char_start, char_end)` plus identity and role — so that
"which works cite TDNT s.v. *deror*?" is a query, every quote is machine-checked,
and a published sentence descends to characters in a source.

---

## 1. Verified state (2026-09-04, `main` @ `3b5251d`)

| Capability | Status | Bearing on this doc |
|---|---|---|
| `document_texts` + normalization, trigram index | **built** (mig. 003) | the substrate citations address |
| Real `char_start`/`char_end` offsets, span index | **built** (mig. 004) | the anchor |
| `verify_quote` → exact/normalized/near/not_found + span + locator | **built** | the checker; verified end-to-end (guide II P3-5) |
| Structural node tree, locators, entities, typed edges | **built** | retrieval and traversal |
| `argument.claims` / `claim_edges` / `anchors` (migration 009) | **not built** | claim refs unresolvable today |
| `bibliographic_records` / `bib_contributors` / `works` (P3-1) | **not built** | identity lives in untyped `documents.metadata` JSON |
| Bibliographic display | **not built** | free text is currently the *only* option |
| `works/` directory + front-matter contract | **built** (this branch, `works/README.md`) | the file half |

Conclusion: the span half of citations is **usable today**. The identity half is
the gap. The design must make the file contract stable now, and light up identity
and claims as 009 and P3 land — without rewriting a single work file.

---

## 2. The one rule everything else follows

> **One anchoring rule, everywhere: `(document_id, char_start, char_end)`.**

The vision doc states it for claim evidence (§2: "Never to `passage_id`"; passages
are derived and re-chunked away; `document_texts.text` is stable). The same
address appears in four places, and it is the *same* address:

```
argument.anchors        claim evidence        (migration 009)
core.work_citations     work citations        (this doc)
core.annotations        human margin notes    (guide II P4-1)
assets / content_kind   figures, tables       (gaps §8.1)
```

Because all four share one address space, joins between them are span overlap —
no mapping layers, no "link fields you have to remember to fill." A work citation
and a claim anchor over the same span resolve to each other by geometry, not by
bookkeeping. That join is the whole feature: *this essay renders the evidence of
that claim* becomes true by construction.

Everything below is this rule plus two facts about where authority lives.

---

## 3. Authority: files own citations, the database mirrors them

The tension: works must stay files (versioned, diffable, drafts live outside the
engine — vision §6), but citations must be queryable with FK integrity.

The house answer is the pattern already proven twice in this codebase —
`document_texts` vs. `passages`, and `anchors.passage_id` as a cache:

> **The work file is the authority for what it cites. The database holds a
> derived, rebuildable projection of it.**

- Front-matter is written by the author (or by the agent, which then has the
  author confirm it — the claims discipline: the model proposes, the human
  commits).
- `work_index` parses front-matter, verifies each span, resolves identity, and
  writes `core.work_citations` rows.
- Re-running `work_index` is idempotent; a stale index is *detected*, not
  assumed fresh, via a front-matter hash.

Nothing structured is invented in the DB that the file does not state, and
nothing in the file needs the DB to exist. Deleting the DB projection loses
nothing; deleting the file orphans the rows, which is a lint error, not data loss.

### Options rejected

- **B — citations as DB rows only** (works ingested as documents, citations as
  annotations). Rejected: puts Layer-4 churn into Layer-2 schema, kills plain-text
  diffing of drafts, and contradicts §6's division of labour.
- **C — sidecar index per work** (SQLite/JSON next to the file). Rejected: a
  second query engine invisible to `doctor`, backups, and the MCP surface.
- **A — free text with a style guide.** Rejected: that is the status quo; a style
  guide cannot be queried, and humans drift from style guides under deadline.

---

## 4. Schema

One additive migration, `010_work_citations`. Two tables, both derived caches —
note what they are *not*: they create no new truth about the corpus, they only
reference documents that already exist.

```sql
-- One row per work file. The mirror of the file's front-matter header.
CREATE TABLE core.works_index (
  work_path        text PRIMARY KEY,      -- 'works/deror-leviticus-25-translation.md'
  title            text NOT NULL,
  work_type        text NOT NULL,         -- translation | essay | dossier | script | outline
  status           text NOT NULL,         -- draft | review | published
  claim_refs       text[] NOT NULL DEFAULT '{}',   -- 'JUB-004'; unresolved until 009
  front_matter_sha text NOT NULL,         -- drift detection
  indexed_at       timestamptz NOT NULL DEFAULT now()
);

-- One row per front-matter citation entry.
CREATE TABLE core.work_citations (
  work_path     text NOT NULL REFERENCES core.works_index(work_path) ON DELETE CASCADE,
  citation_id   text NOT NULL,            -- 'c1', the [^c1] handle
  role          text NOT NULL,            -- asserts | supports | rebuts | context

  -- The durable anchor. Same rule as argument.anchors:
  -- RESTRICT — you may not delete a source a work cites.
  document_id   uuid NOT NULL REFERENCES core.documents(id) ON DELETE RESTRICT,
  char_start    integer NOT NULL,
  char_end      integer NOT NULL,
  quoted_text   text NOT NULL,

  -- Verification provenance, recorded at index time.
  verify_status  text NOT NULL,           -- exact | normalized | near | not_found
  parser_version text,

  -- Identity. zotero_key is the bridge until P3-1; bib_record_id is added by
  -- P3's migration (bibliographic_records is keyed on document_id).
  zotero_key    text,
  locator       jsonb NOT NULL DEFAULT '{}',

  PRIMARY KEY (work_path, citation_id),
  CONSTRAINT wc_span_ck CHECK (char_end > char_start)
);
CREATE INDEX wc_document_idx ON core.work_citations (document_id);
CREATE INDEX wc_work_idx     ON core.work_citations (work_path);
```

Design notes, each earning its place:

- **`verify_status NOT NULL`, including `not_found`.** The index mirrors the
  file honestly — a broken citation is data about the work. The *gate*, not the
  index, refuses to publish it. An index that hides failures teaches the user to
  distrust it (the same honesty rule as guide II P3-5: "no canonical text" is a
  different answer from "not found").
- **`ON DELETE RESTRICT` on `document_id`.** Same constraint as `argument.anchors`
  (vision §2): the corpus cannot silently drop a source a work cites. If a source
  must go, the works citing it must be resolved first — loudly.
- **`claim_refs` as text, not FK.** Migration 009 does not exist; refs must be
  storable now. After 009, a view joins `ref → argument.claims.ref` and
  unresolved refs are a lint finding, not a constraint violation — a typo in a
  ref should not require a migration to discover.
- **`zotero_key` duplicated, deliberately.** It is stored on the work (what the
  author intends) and derivable from the document (`documents.metadata` by
  ingest convention — vision §5). A mismatch between the two is exactly the kind
  of silent error this architecture exists to surface, so `work_verify` checks it.

---

## 5. The identity bridge (until P3)

Today, bibliographic identity is whatever shape each ingest plugin left in
`documents.metadata` (gaps finding 14: zero hits for CSL/Zotero/BibTeX anywhere).
That is a display problem only until someone formats a bibliography — then it
becomes a correctness problem ("which edition's p. 214?" — vision §5).

Bridge, three steps, no file rewrites:

1. **Now:** front-matter carries `zotero_key` (+ free-text `edition`, `locator`).
   Display strings, when needed, are built from `documents.metadata` and labeled
   provisional.
2. **Ingest convention:** plugins record `metadata.zotero_key` per source
   document (one line per pack; the seed-corpus Zotero authority already exists).
3. **When P3-1 lands:** `bibliographic_records.citekey`/`doi` become the identity;
   `work_index` populates `bib_record_id`; display moves to the Tier-1 inline
   formatter (gaps §3.4 Option 4 — built from rows, never hand-typed). The
   front-matter fields stay exactly where they are; they were chosen to be P3's
   columns from day one.

**Do not build CSL rendering now.** The two-tier decision is already made
(Option 4): inline refs from rows for orientation, CSL-JSON at the boundary for
styles. It is deferred behind P3-1 like everything else that needs it.

---

## 6. Tool surface — four tools, pulled by real work

Same MCP container as `verify_quote`; nothing new is invented, the tools are thin
compositions over existing pieces:

1. **`work_verify [path]`** — parse front-matter; run `verify_quote` per citation;
   check every claim ref against `argument.claims` (all unresolved before 009 —
   reported as such, not as errors); check `zotero_key` agreement with the
   document's metadata. Exit nonzero on anything blocking review. This is the
   tool that makes the README's contract enforceable instead of aspirational.
2. **`work_index`** — rebuild `core.works_index` / `core.work_citations` from
   `works/*.md`. Idempotent; reports drift via `front_matter_sha`.
3. **`work_citations --document <id> | --claim <ref> | --zotero <key>`** — the
   leverage query, works-flavoured: which created works rest on this source, this
   claim, this bibliography entry. Before 009 the `--claim` form answers from
   `claim_refs`; after, it joins the ledger.
4. **`work_render [path]`** — the *only* place display strings are built: footnote
   blocks from stored rows (identity + locator), provisional today, Tier-1
   formatted post-P3. Free text is permitted at exactly one boundary —
   machine-generated output — and nowhere as input.

The publication gate lives in `work_verify`, not in the files: a work may flip to
`published` only when every citation verifies `exact`/`normalized` and every claim
ref resolves. After 009 this gate composes with the ledger's rules (§10 of the
program doc: "publication is gated, not remembered") — `work_verify` consults
claim status, so a work resting on a `refuted` premise cannot pass review.

---

## 7. Worked example — the Greek-translation brick

`works/deror-leviticus-25-translation.md` — a translation of Lev 25 with the
*deror* word-study as its spine.

```yaml
claims: [JUB-001, JUB-004]
citations:
  - id: c1
    document_id: <tdnt-doc-uuid>
    char_start: 3326
    char_end: 3546
    quoted_text: "…"            # the TDNT deror entry, as scanned
    role: asserts
    zotero_key: TDNT_1964
    locator: {volume: II, page: 64}
  - id: c2
    document_id: <bhs-doc-uuid>
    char_start: 40211
    char_end: 40255
    quoted_text: "וְקִדַּשְׁתֶּם אֵת שְׁנַת הַחֲמִשִּׁים שָׁנָה"
    role: supports
    zotero_key: BHS_1997
    locator: {verses: "Lev 25:10"}
```

What this buys, concretely, per question someone will actually ask:

- *"Does the translation's reading of v. 10 rest on the right words?"* —
  `work_verify` re-runs `verify_quote` against `document_texts`; the BHS span
  verifies `exact`, the TDNT scan `normalized` (OCR noise), and the report says
  which is which.
- *"Which of my works lean on TDNT's deror entry?"* — `work_citations --document
  <tdnt-uuid>`, today, no migration needed.
- *"What does JUB-004 rest on?"* — the claim's `argument.anchors` and this work's
  citations are rows over the same address space; overlap join renders the
  evidence bundle for the script, the dossier, and the paper from one store.
- *"Which edition of TDNT?"* — `zotero_key` resolves to the Zotero record now; to
  `bibliographic_records` when P3 lands; the work file never changes.

This is the §10 property — descent from a published sentence to characters —
arriving in the works layer *before* the ledger exists, on the same coordinates,
so nothing is migrated when the ledger arrives.

---

## 8. Sequencing

| Step | Trigger | Cost |
|---|---|---|
| `works/` contract (README + template) | done | done |
| `work_verify` + `work_index` + `work_citations` tools | now — `verify_quote` already works | ~1 day; no migration |
| `010_work_citations` migration | when the first real work exists and grep stops being enough (roughly: >3 works or >20 citations) | small, additive |
| `bib_record_id` + Tier-1 inline display | P3-1 (deferral schedule: first formatted bibliography) | rides P3 |
| claim-ref resolution + gate composition | migration 009 (program doc week 2) | rides 009 |
| `work_render` CSL tier | first academic submission | rides P3-3 |

The tools are worth building **now**, before the migration: they cost an
afternoon, they run against what exists, and — per the program doc §8 — the point
of doing the smallest thing first is to find the schema defects while they are
cheap. Three real works indexed by hand before `010` is written is the honest
test, and it is the same test §8 of the program doc prescribes for the ledger.

---

## 9. Non-goals

- **No CSL formatter, no citeproc bundling** — decided in gaps §3.4; revisited
  only on a real journal-style request.
- **No Obsidian/Notion sync** — one-way markdown export at most (P4-1's line).
- **No LLM citation extraction at write time** — an agent may *propose*
  front-matter entries; the author commits them; `work_verify` is the contract.
  An unverified citation must be structurally incapable of reaching `published`,
  the same rule as `claim_upsert` refusing `not_found` anchors (program doc §4).
- **No annotation model here** — P4-1 owns annotations; they share the anchoring
  rule, nothing more.

---

## Appendix — tests

1. **Tier honesty** — a citation matching only after normalization reports
   `normalized`, never `exact` (guide II Appendix B #4, reused).
2. **Index mirror** — `work_index` over a fixture work yields exactly the
   front-matter rows; a second run changes nothing.
3. **RESTRICT** — deleting a document cited by an indexed work fails while the
   citation rows exist.
4. **Drift detection** — editing front-matter without re-indexing flips
   `front_matter_sha`; `work_verify` reports the work stale.
5. **Publish gate** — a work with one `near` citation or one unresolved claim ref
   cannot pass `work_verify --gate publish`.
6. **Overlap join** (post-009) — a work citation sharing a span with a claim
   anchor resolves to that claim's ref via span overlap, with no manual link
   recorded anywhere.
