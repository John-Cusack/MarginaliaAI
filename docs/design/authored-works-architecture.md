# Architecture: Authored works with structured citations and provenance

**Status:** Proposed / RFC
**Audience:** core maintainers, pack authors, and research-workflow implementers
**Scope:** authored works, revisions, source links, citations, claim links, export,
and the boundary with the corpus, projects, and bibliographic identity
**Depends on:** canonical document text and spans (built); bibliographic identity
(proposed P3); projects and claims (proposed P4/P5)
**Revises:** `research-program-on-marginalia.md` §6 and §10, which place all
drafts and outputs outside the engine

---

## 1. Executive decision

Marginalia should treat a researcher's created works as first-class, revisioned
research objects. A paper, translation, chapter, lecture, or dossier is not merely a
text file with a bibliography appended to it. It is the top layer of the provenance
graph: its blocks make claims, translate source text, discuss entities, and cite exact
editions and source spans.

The design has four load-bearing decisions:

1. **Source documents and authored works are different aggregates.** Source documents
   remain immutable evidence in `core`. Authored works are mutable, revisioned objects
   in a separate `authored` schema. Do not overload `core.documents` to represent both.
2. **Blocks, not character offsets, are the durable address inside authored work.** A
   draft changes too often for whole-document offsets to remain stable. Each revision
   contains ordered blocks, and a stable `block_key` follows the same logical block
   across revisions.
3. **Citations and semantic links are rows with foreign keys.** Prose may contain an
   opaque citation marker, but a title, DOI, page, verse, or quotation encoded only in
   prose is not a citation in this system.
4. **Publication files are projections.** Markdown, DOCX, LaTeX, HTML, and CSL-JSON
   are rendered from a frozen revision. The database model is the authoritative
   semantic form; exports are portable deliverables.

This makes the desired invariant possible:

> Every publishable assertion or translation decision can be traversed from the
> authored block, through a typed link or citation, to a named edition and, when the
> source is in the corpus, to the exact characters that support it.

---

## 2. Product requirement

The engine must provide a place for work created *from* the corpus, not only a place
for the corpus itself. Created works include at least:

- articles, essays, chapters, books, lectures, and video scripts;
- research dossiers, annotated bibliographies, and literature reviews;
- translations and textual notes;
- argument maps expressed as prose;
- reusable fragments that may later be assembled into a larger work.

A created work must support questions that free text cannot answer reliably:

- Which of my paragraphs depend on this source, edition, claim, or lexicon entry?
- Where did I cite this work, and with which page or passage locator?
- Which translation decisions render a particular Greek lemma?
- Which citations contain a quote whose source text has changed since verification?
- Which published statements depend on a claim now marked open, weakened, or rebutted?
- Generate the notes and bibliography for this revision in a selected CSL style.
- Export a review packet containing every assertion and the evidence behind it.

These are graph queries over authored structure and evidence. They cannot be recovered
dependably by parsing finished prose after the fact.

---

## 3. Goals and non-goals

### 3.1 Goals

- Give every created work and logical block a stable identity.
- Preserve explicit revisions and make published revisions immutable.
- Represent citations independently of their rendered punctuation.
- Link authored blocks to exact corpus spans, claims, and entities with typed roles.
- Preserve work/edition identity and locators separately.
- Support source-backed and bibliography-only citations.
- Validate provenance before a revision can be frozen or published.
- Search authored work without confusing it with external evidence.
- Export deterministic publication artifacts and a portable semantic manifest.
- Allow domain packs to add block types and validation without owning the base model.

### 3.2 Non-goals for the first implementation

- A collaborative, real-time word processor.
- Google Docs or Obsidian bidirectional synchronization.
- General-purpose desktop publishing or layout.
- A universal TEI editor.
- Automatic correctness judgments about an interpretation or translation.
- Storing every keystroke as a revision.
- Replacing Zotero as a bibliographic acquisition and metadata-curation interface.

The first user is still a single researcher working through MCP, CLI, and files. The
model must not prevent a later UI, but a UI is not required to validate the model.

---

## 4. Context and boundaries

### 4.1 Source corpus versus authored work

The two aggregates have different authority and lifecycle:

| Concern | Source corpus | Authored work |
|---|---|---|
| Authority | What an external source says | What the researcher currently writes |
| Mutation | Re-parse/re-ingest under controlled provenance | Edited frequently |
| Stable address | `(document_id, char_start, char_end)` | `(work_id, block_key, revision)` |
| Search role | Evidence and discovery | Synthesis and output |
| Deletion | Restricted when cited | Normally archived; drafts may be discarded |
| Versioning | Parser and canonical-text version | Explicit authored revisions |
| Publication | Usually prohibited or limited by license | Intended output |

Reusing `core.documents` for authored work would make these rules conditional
throughout ingestion, deletion, provenance, and search. A separate aggregate keeps
each invariant simple.

An exported and formally published authored revision may later be ingested as a
`core.document` for citation by another project. That is a deliberate publication
event with a lineage link; it is not how drafts are stored.

### 4.2 Where personal works live

The application repository should define and implement authored works, but a user's
private works should not be committed into the engine's source-code repository by
default. They may contain licensed quotations, unpublished writing, and personal
research.

The canonical semantic state lives in Postgres. A work can also be exported as a
portable directory under a configured workspace, for example:

```text
research-workspace/
  works/
    greek-john-prologue/
      work.yaml
      revision-0007.md
      citations.json
      provenance.json
      exports/
        revision-0007.docx
        revision-0007.pdf
```

That directory may be versioned in a separate private Git repository. It is an export
and interchange boundary, not a second database that must be synchronized silently.

### 4.3 Relationship to projects

A project answers *what research activity is in scope*. A work answers *what the
researcher is producing*. A project may contain many works, and a work may optionally
be reused by more than one project. Model this as membership rather than placing a
single `project_id` on the work.

### 4.4 Relationship to claims

Claims model propositions and their evidence. Work blocks model authored expression
and structure. A block may:

- assert one or more claims;
- summarize or explain a claim;
- rebut or concede a claim;
- depend on claims without reproducing their wording.

Do not turn every paragraph into a claim automatically. The relationship is explicit
and many-to-many.

### 4.5 Relationship to annotations

An annotation is private marginalia about a source span. A work block is content meant
to participate in an authored artifact. Promoting an annotation to a work block should
copy or link its source span, not mutate the annotation into a different object.

---

## 5. Conceptual architecture

```text
 SOURCES                  RESEARCH FACT BASE               AUTHORED OUTPUT

 external source          bibliographic identity           work
      |                         |                            |
      v                         v                            v
 core.document ------> bibliography.edition       authored.work_revision
      |                         ^                            |
      v                         |                            v
 core.document_text      evidence.source_span <--- authored.work_block
      |                         ^                       |    |    |
      +-- char range ----------+                       |    |    |
                                                       |    |    |
                                   structured citation +    |    + claim link
                                   semantic source link ----+          |
                                   entity link ------------------------+-- graph

                                               frozen revision
                                                      |
                                                      v
                                      Markdown / DOCX / LaTeX / HTML
```

There are three different relationships in the authored layer and they should remain
different:

1. **Citation occurrence:** where and how a source is cited in rendered output.
2. **Source link:** what semantic role a source span plays for a block, whether or not
   a visible footnote is rendered there.
3. **Claim/entity link:** which proposition or concept the block expresses or discusses.

Collapsing all three into a generic edge makes simple queries ambiguous and gives up
foreign-key integrity.

---

## 6. Stable identity and revision model

### 6.1 Work

A work is the long-lived identity of an authored artifact: *A Translation and
Commentary on John 1:1–18*. It owns metadata and a sequence of revisions.

Suggested fields:

```sql
CREATE SCHEMA authored;

CREATE TABLE authored.works (
    id                 uuid PRIMARY KEY,
    slug               text NOT NULL UNIQUE,
    title              text NOT NULL,
    work_type          text NOT NULL,
    status             text NOT NULL DEFAULT 'draft',
    language           text,
    abstract           text,
    current_revision_id uuid,
    metadata           jsonb NOT NULL DEFAULT '{}',
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    archived_at        timestamptz,
    CONSTRAINT works_status_ck CHECK
      (status IN ('draft','review','published','archived'))
);
```

`work_type` is an extensible vocabulary: `article`, `book`, `chapter`, `translation`,
`lecture`, `script`, `dossier`, and pack-defined values. It must not be used as a bag
of behavior switches in core. Packs register validation and export extensions for
types that need them.

### 6.2 Revision

A revision is a coherent snapshot suitable for review, citation, or publication.

```sql
CREATE TABLE authored.work_revisions (
    id                uuid PRIMARY KEY,
    work_id           uuid NOT NULL
                      REFERENCES authored.works(id) ON DELETE CASCADE,
    revision_number   integer NOT NULL,
    parent_revision_id uuid
                      REFERENCES authored.work_revisions(id) ON DELETE RESTRICT,
    state             text NOT NULL DEFAULT 'draft',
    message           text,
    content_hash      bytea,
    created_by        text NOT NULL DEFAULT 'user',
    created_at        timestamptz NOT NULL DEFAULT now(),
    frozen_at         timestamptz,
    published_at      timestamptz,
    metadata          jsonb NOT NULL DEFAULT '{}',
    UNIQUE (id, work_id),
    UNIQUE (work_id, revision_number),
    CONSTRAINT work_revision_state_ck CHECK
      (state IN ('draft','frozen','published','superseded'))
);

ALTER TABLE authored.works
  ADD CONSTRAINT works_current_revision_fk
  FOREIGN KEY (current_revision_id, id)
  REFERENCES authored.work_revisions(id, work_id)
  DEFERRABLE INITIALLY DEFERRED;
```

Rules:

- A draft revision may be edited in place.
- `freeze` computes its content hash, runs validation, and makes its content and links
  immutable.
- Publishing is a state change on a frozen revision, not an implicit export side effect.
- Editing a frozen or published revision creates a child revision by copying its block
  snapshot and structured links.
- Revision numbers are display identifiers; UUIDs are authoritative.
- The composite foreign key guarantees that `current_revision_id`, when set, belongs
  to the same work. Deleting the current revision is restricted; select another current
  revision first.

### 6.3 Blocks

Blocks are the smallest independently addressable authored units. Typical kinds are
`heading`, `paragraph`, `quotation`, `translation_unit`, `footnote`, `table`, and
`figure_caption`.

```sql
CREATE TABLE authored.work_blocks (
    id              uuid PRIMARY KEY,
    revision_id     uuid NOT NULL
                    REFERENCES authored.work_revisions(id) ON DELETE CASCADE,
    block_key       uuid NOT NULL,
    parent_id       uuid,
    position        integer NOT NULL,
    block_type      text NOT NULL,
    title           text,
    body_markdown   text NOT NULL DEFAULT '',
    attributes      jsonb NOT NULL DEFAULT '{}',
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, revision_id),
    UNIQUE (revision_id, block_key),
    UNIQUE NULLS NOT DISTINCT (revision_id, parent_id, position),
    FOREIGN KEY (parent_id, revision_id)
      REFERENCES authored.work_blocks(id, revision_id) ON DELETE RESTRICT
);
CREATE INDEX work_blocks_revision_idx
    ON authored.work_blocks(revision_id, parent_id, position);
```

`id` identifies a block snapshot in one revision. `block_key` identifies the same
logical block across revisions. Copying a revision preserves `block_key`; splitting a
block preserves it on one side and creates a new key for the other; merging blocks
records predecessor keys in `attributes` for auditability.

The composite foreign key rejects a `parent_id` from another revision. Parent deletion
is `RESTRICT`: removing an entire subtree must be an explicit operation, not an
accidental cascade from deleting its heading.
`NULLS NOT DISTINCT` is intentional and requires Postgres 15; without it, two root
blocks could occupy the same position because ordinary unique constraints treat each
`NULL` as different.

### 6.4 Why authored blocks do not use document-relative offsets

Canonical source text is stored once and changes rarely, so a character range is a
good source address. Authored prose changes continuously: inserting a title at the
start would invalidate every later offset. Stable logical blocks constrain churn to
the block being edited and give semantic links a durable home.

Within a block, avoid using offsets as the only location for a citation. Use a stable
inline marker such as `{{cite:6f91...}}`; the referenced row carries the semantics.
The renderer replaces the marker. A validator rejects missing and duplicate markers.

---

## 7. Shared evidence spans

Claims, annotations, and authored works all need the same source-side primitive. The
existing plans define similar span columns independently. Before adding a third copy,
promote the concept into a reusable table.

```sql
CREATE SCHEMA evidence;

CREATE TABLE evidence.source_spans (
    id                    uuid PRIMARY KEY,
    document_id           uuid NOT NULL
                          REFERENCES core.documents(id) ON DELETE RESTRICT,
    char_start            integer NOT NULL,
    char_end              integer NOT NULL,
    quoted_text           text,
    normalized_quote_hash bytea,
    verify_status         text,
    verified_at           timestamptz,
    parser                text,
    parser_version        text,
    locator               jsonb NOT NULL DEFAULT '{}',
    passage_id            uuid
                          REFERENCES core.passages(id) ON DELETE SET NULL,
    created_at            timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT source_spans_range_ck CHECK
      (char_start >= 0 AND char_end > char_start),
    CONSTRAINT source_spans_verify_ck CHECK
      (verify_status IS NULL OR verify_status IN ('exact','normalized','near'))
);
CREATE INDEX source_spans_document_range_idx
    ON evidence.source_spans(document_id, char_start, char_end);
```

The durable address is `(document_id, char_start, char_end)`. `passage_id` is only a
cache and remains `SET NULL` during re-chunking. A parser-version change marks the span
for re-verification; it does not silently rewrite the address.

`quoted_text` is a verification snapshot, not permission to republish the source.
Export policy must still consult the document's rights metadata.

Claim anchors, annotations, and authored source links should reference
`evidence.source_spans.id`. If existing feature-specific anchor tables land first,
provide a migration into this shared primitive rather than allowing their semantics to
diverge.

---

## 8. Bibliographic identity and citations

### 8.1 Required bibliographic contract

Citation rendering cannot use `core.documents.metadata['author']` as its authority.
The bibliographic layer must distinguish:

- the abstract work, such as BDAG;
- the edition or manifestation actually consulted;
- contributors and their roles;
- identifiers such as DOI, ISBN, OCLC, or Zotero key;
- CSL-compatible metadata used for rendering.

This RFC does not replace the P3 bibliographic design. It requires the following
stable interface from it:

```text
bibliography.work
  └── bibliography.edition
        ├── identifiers
        ├── contributors
        └── CSL representation

core.document ── represents ──> bibliography.edition
```

Zotero may remain the editing authority initially. Import its stable key and metadata
into the local bibliographic record. Authored citations reference the local UUID, not
an unvalidated string, while preserving the Zotero key for round-trip updates.

### 8.2 Citation occurrence and items

A citation occurrence is the location and intent of a citation in an authored block.
It may contain one or more cited items.

```sql
CREATE TABLE authored.citation_occurrences (
    id              uuid PRIMARY KEY,
    citation_key    uuid NOT NULL,
    block_id        uuid NOT NULL
                    REFERENCES authored.work_blocks(id) ON DELETE CASCADE,
    placement       text NOT NULL DEFAULT 'inline',
    intent          text NOT NULL DEFAULT 'source',
    note            text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (block_id, citation_key),
    CONSTRAINT citation_placement_ck CHECK
      (placement IN ('inline','block_end')),
    CONSTRAINT citation_intent_ck CHECK
      (intent IN ('source','support','contrast','background','definition',
                  'translation','quotation','see_also'))
);

CREATE TABLE authored.citation_items (
    occurrence_id        uuid NOT NULL
                         REFERENCES authored.citation_occurrences(id)
                         ON DELETE CASCADE,
    position             integer NOT NULL,
    edition_id           uuid NOT NULL
                         REFERENCES bibliography.editions(id) ON DELETE RESTRICT,
    source_span_id       uuid
                         REFERENCES evidence.source_spans(id) ON DELETE RESTRICT,
    locator              jsonb NOT NULL DEFAULT '{}',
    prefix               text,
    suffix               text,
    suppress_author      boolean NOT NULL DEFAULT false,
    PRIMARY KEY (occurrence_id, position)
);
```

The `citation_key` survives when a revision is copied; each revision receives new row
IDs but preserves logical identity. `body_markdown` places it with
`{{cite:<citation_key>}}`.

`locator` is structured rather than formatted text. Examples:

```json
{"page": "85"}
{"page_start": "85", "page_end": "87"}
{"section": "2.3", "paragraph": "4"}
{"book": "John", "chapter": 1, "verse_start": 1, "verse_end": 3}
{"entry": "logos", "sense": "2a"}
```

Core validates the generic shape and retains unknown keys. Packs may register stricter
locator schemas for particular document or edition types.

### 8.3 Citation invariants

- Every citation item references an edition.
- A corpus-backed citation may additionally reference one exact source span.
- If a source span is present, its document must represent the cited edition. Reject a
  span from one edition paired with the bibliographic identity of another.
- A visible citation marker must resolve to exactly one occurrence in that block.
- An occurrence must either be placed by a marker or explicitly marked
  `placement='block_end'`; there are no invisible orphan citations.
- A quotation citation cannot be frozen unless the source span is verified `exact` or
  `normalized`; `near` requires an explicit override recorded in the decision log.
- Rendered citation strings are never stored as the authoritative citation.

Bibliography-only citations are legitimate when the source has not been ingested.
They are less strongly grounded and must be reported as such by validation.

---

## 9. Semantic links from authored blocks

### 9.1 Source links

Source links express what a block does with source text independently of whether a
footnote is rendered.

```sql
CREATE TABLE authored.block_source_links (
    block_id       uuid NOT NULL
                   REFERENCES authored.work_blocks(id) ON DELETE CASCADE,
    source_span_id uuid NOT NULL
                   REFERENCES evidence.source_spans(id) ON DELETE RESTRICT,
    relation       text NOT NULL,
    confidence     real,
    note           text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (block_id, source_span_id, relation),
    CONSTRAINT block_source_conf_ck CHECK
      (confidence IS NULL OR confidence BETWEEN 0 AND 1)
);
```

Core relations should include `quotes`, `paraphrases`, `translates`, `summarizes`,
`discusses`, `supports`, `contrasts`, and `defines`. Packs may contribute vocabulary
but not redefine an existing relation silently.

### 9.2 Claim links

```sql
CREATE TABLE authored.block_claim_links (
    block_id    uuid NOT NULL
                REFERENCES authored.work_blocks(id) ON DELETE CASCADE,
    claim_id    uuid NOT NULL
                REFERENCES argument.claims(id) ON DELETE RESTRICT,
    relation    text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (block_id, claim_id, relation)
);
```

Relations include `asserts`, `explains`, `depends_on`, `rebuts`, `concedes`, and
`qualifies`. These are expression relationships, not a second claim graph. Claim-to-
claim support and contradiction remain in `argument.claim_edges`.

### 9.3 Entity links

```sql
CREATE TABLE authored.block_entity_links (
    block_id    uuid NOT NULL
                REFERENCES authored.work_blocks(id) ON DELETE CASCADE,
    entity_id   uuid NOT NULL
                REFERENCES core.entities(id) ON DELETE RESTRICT,
    relation    text NOT NULL,
    surface_form text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (block_id, entity_id, relation)
);
```

Entity links make lexemes, people, places, texts, and concepts addressable without
requiring a claim for every mention. For translation work, a lemma can be an entity and
a translation block can `renders` or `discusses` it.

### 9.4 Why not use `core.edges` for all of this

`core.edges` has polymorphic `(kind, id)` endpoints that Postgres cannot protect with
foreign keys. Authored work is intended to become publishable and should not tolerate
dangling evidence or claim links. Small typed join tables are repetitive but make the
important promises enforceable by the database.

---

## 10. Greek translation example

Consider a work titled *A Translation and Commentary on John 1:1–18*. Its hierarchy
might be:

```text
work: greek-john-prologue
  revision: 7 (draft)
    heading: John 1:1
      translation_unit: clause-a
      translation_unit: clause-b
      commentary: logos-sense
    heading: John 1:2
      ...
```

The `clause-b` block contains the researcher's translation:

```text
and the Word was God. {{cite:4ef0...}} {{cite:82c1...}}
```

Its structured relationships are:

```text
block clause-b
  translates  -> source span containing καὶ θεὸς ἦν ὁ λόγος
  renders     -> entity:lemma:λόγος
  renders     -> entity:lemma:θεός
  asserts     -> claim: the preverbal anarthrous θεός is qualitative here

citation 4ef0
  edition     -> Greek New Testament, consulted edition
  source span -> John 1:1 Greek text
  locator     -> {book: John, chapter: 1, verse: 1}
  intent      -> translation

citation 82c1
  edition     -> a specific grammar or commentary edition
  source span -> the exact supporting discussion in the ingested resource
  locator     -> {page: 266, section: "Colwell construction"}
  intent      -> support
```

This permits queries such as:

```sql
-- Every block that renders λόγος, across works and revisions.
SELECT w.slug, r.revision_number, b.block_key, b.body_markdown
FROM authored.block_entity_links bel
JOIN authored.work_blocks b ON b.id = bel.block_id
JOIN authored.work_revisions r ON r.id = b.revision_id
JOIN authored.works w ON w.id = r.work_id
WHERE bel.entity_id = :logos_lemma_id
  AND bel.relation = 'renders';

-- Published blocks affected by a source parser change.
SELECT DISTINCT w.slug, r.revision_number, b.block_key
FROM authored.block_source_links bsl
JOIN evidence.source_spans s ON s.id = bsl.source_span_id
JOIN core.document_texts dt ON dt.document_id = s.document_id
JOIN authored.work_blocks b ON b.id = bsl.block_id
JOIN authored.work_revisions r ON r.id = b.revision_id
JOIN authored.works w ON w.id = r.work_id
WHERE r.state = 'published'
  AND s.parser_version IS DISTINCT FROM dt.parser_version;
```

The important result is not a specialized “Greek note” blob. It is a normal authored
block using stable, typed relations. A biblical-studies pack may add stricter
validation—for example, requiring every `translation_unit` to have exactly one
`translates` source link—without changing the core schema.

---

## 11. Services and ports

The authored subsystem should follow the repository/service split already used by the
engine.

### 11.1 Repositories

- `WorkRepo` — work lifecycle and project membership.
- `WorkRevisionRepo` — copy, freeze, publish, and retrieve snapshots.
- `WorkBlockRepo` — ordered tree operations and block-key lookup.
- `CitationRepo` — occurrences, cited items, and marker validation.
- `SourceSpanRepo` — create, resolve, and find spans needing verification.
- `WorkLinkRepo` — typed source, claim, and entity links.

Repositories enforce persistence invariants; services orchestrate multi-table actions
inside one transaction.

### 11.2 Services

- `WorkService` — create work, begin revision, upsert/reorder blocks, archive.
- `CitationService` — resolve bibliographic identity, verify source quote, and attach a
  citation occurrence atomically.
- `WorkValidationService` — run structural, citation, provenance, and policy checks.
- `WorkPublicationService` — freeze, render, hash, publish, and record artifacts.
- `WorkSearchService` — FTS/vector retrieval over current or selected revisions.
- `WorkTraceService` — traverse block → claims/citations/spans → documents.

`CitationService.attach` is the important write boundary. Callers should not need to
coordinate five independent inserts and risk leaving a half-citation behind.

---

## 12. MCP and CLI surface

Start with a narrow tool surface that supports a real work end to end.

### 12.1 Minimum MCP tools

1. **`work_create`** — create the stable work and its first draft revision.
2. **`work_get`** — return ordered blocks plus structured links; selectable revision.
3. **`work_block_upsert`** — add or update a block with optimistic concurrency.
4. **`work_cite`** — resolve/create the edition, verify an optional quote, create the
   source span, citation occurrence, citation item, and marker in one transaction.
5. **`work_link`** — attach a source span, claim, or entity with a typed relation.
6. **`work_trace`** — return the complete provenance tree for a block, revision, or
   work.
7. **`work_validate`** — return errors, warnings, and publication blockers.
8. **`work_freeze`** — validate and freeze a revision.
9. **`work_export`** — render a frozen revision and write an artifact manifest.

Later tools may add revision diffs, impact analysis, project dashboards, and bulk
translation consistency checks. They should be pulled by observed workflows.

### 12.2 Concurrency

Every mutable command accepts `expected_updated_at` or a revision ETag. A stale caller
receives a conflict instead of silently overwriting a newer agent or filesystem edit.
This is inexpensive in a single-user system and prevents agent races immediately.

### 12.3 CLI

```text
research-engine work create
research-engine work show <slug> [--revision N]
research-engine work validate <slug> [--revision N]
research-engine work freeze <slug>
research-engine work export <slug> --format markdown|docx|latex|html
research-engine work bundle <slug> --output <directory>
research-engine work import-bundle <directory> --dry-run
```

---

## 13. Validation and publication gates

Validation should return stable rule IDs so users can explicitly waive warnings and so
tests can assert behavior without matching prose.

### 13.1 Errors

- `AUTH_PARENT_REVISION_MISMATCH` — block parent belongs to another revision.
- `AUTH_CITATION_MARKER_MISSING` — a citation occurrence has no placement.
- `AUTH_CITATION_MARKER_DANGLING` — marker has no occurrence.
- `AUTH_CITATION_EDITION_MISSING` — citation has no edition identity.
- `AUTH_CITATION_EDITION_MISMATCH` — source document represents another edition.
- `AUTH_QUOTE_UNVERIFIED` — quotation is not exact or normalized.
- `AUTH_SOURCE_SPAN_STALE` — parser version changed since verification.
- `AUTH_CLAIM_DANGLING` — prevented by FK, retained as a diagnostic invariant.
- `AUTH_REVISION_MUTATED` — frozen content does not match its stored hash.

### 13.2 Warnings

- `AUTH_BIBLIOGRAPHY_ONLY` — citation has no corpus-backed source span.
- `AUTH_BLOCK_UNGROUNDED` — a block marked as an assertion or translation has no
  source or claim link.
- `AUTH_OPEN_DEPENDENCY` — publishable block depends on an open claim.
- `AUTH_LICENSE_EXPORT` — requested export includes quotation text that policy does
  not permit.
- `AUTH_UNUSED_CITATION` — occurrence exists but is not visible in the rendered form.

Publication policy decides which warnings become blockers for a work type. For
example, a translation pack may require every `translation_unit` to have a verified
primary-source link, while an informal lecture may permit bibliography-only citations.

Waivers are rows with rule ID, revision ID, actor, reason, and timestamp. They are not
booleans hidden in metadata.

---

## 14. Search and embeddings

Authored blocks should be searchable, but they must not be silently mixed into source
evidence.

- Store authored FTS in an `authored.work_block_fts` table.
- Add block embeddings only when semantic search across authored output is needed.
- Expose an explicit scope: `corpus`, `authored`, or `all`.
- Label every hit with its authority class.
- Default evidence-seeking tools to `corpus`; default writing-reuse tools to
  `authored`.

Do not insert draft blocks as corpus passages simply to reuse the existing search
service. That creates circular evidence: a search for support may return the
researcher's own prose as though it were an external source.

Embedding rows should include revision ID and model identity. Published revisions are
immutable, so their embeddings are reproducible; draft embeddings are disposable and
may be updated asynchronously.

---

## 15. Export and portability

### 15.1 Rendering

An exporter walks the ordered block tree, resolves citation markers, renders citations
through CSL, and emits the selected format. The result records:

- work and revision UUIDs;
- revision content hash;
- exporter and style versions;
- artifact checksum;
- bibliography snapshot hash;
- validation result and waivers;
- creation timestamp.

The same frozen revision and versions must produce semantically identical output.
Byte-identical DOCX or PDF is not required when upstream renderers embed timestamps,
but Markdown/JSON exports should be deterministic.

### 15.2 Portable bundle

The portable bundle contains authored content and identifiers, not licensed source
text. `provenance.json` includes document identities, source-span coordinates,
verification hashes, and permitted short quotation snapshots. It must not export full
corpus passages by default.

An import is explicit and transactional:

1. parse and validate the bundle;
2. resolve work, edition, document, entity, and claim IDs;
3. report unresolved or conflicting identities;
4. show a dry-run diff;
5. write a new revision rather than overwriting published history.

This is intentionally not background bidirectional synchronization.

---

## 16. Security, privacy, and rights

- Authored works are private by default.
- Export is a controlled data-egress path and must be logged.
- A source document's rights classification travels to each source span.
- Public exports include only authored text, rendered bibliographic data, and quotation
  text allowed by policy.
- Work bundles must not include credentials, original licensed files, or full source
  passages.
- Pack validators may inspect authored block metadata but receive source text only
  through the same permission-gated clients used elsewhere.
- Deleting a source document referenced by a work is restricted. Archive it or remove
  the dependent citation explicitly; never silently orphan published provenance.

---

## 17. Failure modes

| Failure | Required behavior |
|---|---|
| Zotero unavailable | Existing local bibliographic records render; metadata refresh fails clearly |
| Source document re-parsed | Affected spans become stale and publication validation fails |
| Source document re-chunked | No authored link breaks; cached `passage_id` is refreshed or nulled |
| Citation style missing | Revision remains valid; requested export fails without changing state |
| LLM unavailable | Manual authoring, citation, validation, and export still work |
| Export renderer fails | Frozen revision remains unchanged; no partial artifact is registered |
| Agent writes stale draft | Optimistic-concurrency conflict; no overwrite |
| Plugin defining a block type is disabled | Content remains readable; specialized validation/export reports unavailable |
| Bibliographic merge occurs | Foreign keys are repointed transactionally; old identity remains in an audit record |

---

## 18. Delivery plan

### Phase A — one grounded authored work

Build only what is necessary to create, edit, inspect, validate, and export one work:

- `authored.works`, revisions, and blocks;
- `evidence.source_spans`;
- minimal edition records imported from Zotero;
- citation occurrences/items;
- block source links;
- `work_create`, `work_get`, `work_block_upsert`, `work_cite`, `work_validate`, and
  Markdown export.

Acceptance test: represent John 1:1–5 with at least five translation blocks, two
primary-text links, two scholarly citations, and one exact quotation. Export Markdown
with resolved notes and walk every citation back to its source.

### Phase B — claims, projects, and publication

- project membership;
- block-to-claim and block-to-entity links;
- revision freeze/publish lifecycle;
- impact analysis from changed claims or stale source spans;
- CSL style selection and bibliography generation;
- portable bundle export/import.

Acceptance test: changing a claim's status or a document's parser version identifies
every affected published block.

### Phase C — domain-specific authoring

- biblical-studies block vocabulary and translation validation;
- translation consistency queries by lemma/entity;
- richer locators for verse, lexicon entry, and manuscript witness;
- DOCX/LaTeX exporters;
- authored-block FTS and optional embeddings.

Acceptance test: query every rendering of one Greek lemma across the work and produce
a review report containing translations, claims, citations, and evidence.

### Explicitly deferred

- real-time collaboration;
- arbitrary bidirectional editor sync;
- automatic prose generation as the canonical authoring path;
- generalized rule engine beyond concrete publication checks;
- block-level embeddings before cross-work retrieval is a measured need.

---

## 19. Test strategy

### 19.1 Unit and contract tests

- revision copy preserves `block_key` and citation identity;
- frozen revisions reject every mutation path;
- citation markers and occurrences are bijective;
- block parents cannot cross revisions;
- citation source span and edition agree;
- exact and normalized quotes pass; near/not-found do not pass silently;
- re-chunking does not affect source links;
- parser-version drift is detected;
- render order follows the block tree deterministically;
- disabled domain validators do not corrupt generic content.

### 19.2 Integration tests

- create a work, cite an ingested fixture, freeze it, and export it;
- roll back the entire `work_cite` transaction after an induced failure;
- re-chunk the cited document and confirm the work remains valid;
- re-parse the document with shifted text and confirm the work becomes stale;
- merge duplicate bibliographic identities without orphaning citation items;
- import an exported bundle into an empty test corpus and report unresolved sources.

### 19.3 Invariants in `doctor`

Add corpus checks for dangling marker references, cross-revision parents, frozen hash
drift, citation/edition mismatch, stale source spans, and published revisions with
blocking validation failures. Sample checks are not enough for FK-like invariants;
these should scan the authored tables or rely on database constraints.

---

## 20. Alternatives rejected

### 20.1 Keep works as Markdown and grep citation keys

This is portable but cannot enforce edition identity, source verification, claim
status, or referential integrity. It makes the most important product behavior an
informal naming convention.

### 20.2 Store authored works as `core.documents`

This reuses ingestion and search, but conflates evidence with synthesis, makes mutable
drafts fight immutable-source assumptions, and allows a researcher's own prose to be
returned as external support.

### 20.3 Use only generic graph edges

This minimizes tables but gives up foreign keys and makes citation-specific fields
awkward. Publishable provenance warrants typed relations.

### 20.4 Anchor authored citations by whole-document character offsets

Offsets cascade after every edit. Revisioned blocks with stable logical keys localize
change and make diffs meaningful.

### 20.5 Make files canonical and synchronize them continuously

Bidirectional sync introduces conflict resolution, partial updates, and parser-version
compatibility before the domain model has been validated. Deterministic export and
explicit transactional import capture most portability value first.

### 20.6 Build a specialized Greek-translation schema in core

Greek translation is the proving use case, not a reason to make core domain-specific.
Generic blocks, spans, citations, claims, and entities cover the shared semantics;
strict translation rules and specialized locators belong in a pack.

---

## 21. Open decisions

These should be answered with the first real translation work, not by speculation:

1. Is block-level Markdown with stable citation markers sufficient, or does editing
   require a structured inline AST?
2. Does Zotero remain the bibliographic write authority, or may Marginalia edit local
   edition records and push them back?
3. Which locator keys are required for biblical texts, lexica, and grammars?
4. Should a work belong to multiple projects immediately, or can membership wait for
   the second reuse case?
5. Which validation warnings block `freeze`, which block only `publish`, and which are
   work-type policies?
6. How much quoted text may a portable bundle include for each rights class?
7. Does the first non-Markdown exporter need DOCX or LaTeX?

The schema should not depend on these choices except where identified above.

---

## 22. Definition of done

The authored-work layer is successful when all of the following are demonstrable on a
real work:

1. A Greek translation is represented as ordered, revisioned blocks rather than one
   undifferentiated text field.
2. Every translation unit can link directly to the Greek source span it translates.
3. Scholarly citations identify a real edition and structured locator.
4. A quotation can be verified against canonical source text before publication.
5. A user can ask where a source, claim, or lemma affects their authored output and get
   a complete answer without searching prose.
6. Re-chunking a source breaks no authored link; re-parsing it makes affected links
   visibly stale.
7. A frozen revision exports with deterministic citations and an auditable manifest.
8. The portable bundle contains enough identity and provenance to reconstruct the work
   without redistributing the private source corpus.

At that point Marginalia is no longer only a system for finding evidence. It is a
system for creating accountable work from evidence.
