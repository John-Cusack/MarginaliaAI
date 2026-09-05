# Running the redistribution research program on Marginalia

**Status:** planning. Written 2026-08-25 against `main` (core 0.5.0+).
**Companion to:** `research-workflow-gaps.md` §5 and §7, `research-workflow-implementation-2.md` P5.

---

## 0. The thesis

The eleven workstreams describe a research program. This engine is not a place to
*keep* that program — Notion, Obsidian, and Zotero all keep things better. What it
does that none of them do is make a claim **addressable**: give it a stable id, bind
it to the exact characters of the text that supports it, and connect it to the other
claims it holds up.

So the division of labour is:

> **The engine holds claims, evidence, and the edges between them.
> Everything else — dossiers, essays, scripts, the book file — is *generated from*
> that and lives outside.**

If that split holds, Workstream 2 (the Claim Ledger) and Workstream 3 (the argument
graph) stop being documents you maintain by hand and become **one dataset with two
views**. The leverage metric you wanted — *"downstream claims affected: 7"* — becomes
a SQL query rather than a thing you remember to update.

Sections 1–9 are the next twelve weeks. **§10 is the endpoint** the twelve weeks must
stay compatible with — including the rules engine (§11), which is deferred but shapes
two decisions in the present.

That is the whole reason to use this repo. If you would not use the leverage query,
use Notion and save yourself six weeks.

---

## 1. What is actually here (verified, 2026-08-25)

The two implementation guides in this folder describe a corpus that was broken in
specific ways. Most of that has since been fixed and the docs were never revised.
Verified against `main`:

| Capability | Status | Relevance to the program |
|---|---|---|
| Hybrid retrieval (vector + FTS + RRF + rerank) | **built**, HNSW index landed (mig. 006) | reading the opposition at passage level |
| Canonical document text | **built** (`document_texts`, mig. 003) | prerequisite for citable quotes |
| `verify_quote` — exact/normalized/near/not_found | **built** | *every ledger row's quotation, checked* |
| Locators + `set_locators` (late attachment) | **built** | pin-cites without re-embedding |
| Structural node tree (`document_nodes`, ltree) | **built** (mig. 007/008) | "which section of Sider is this" |
| Entities + aliases + mentions with spans | **built** | **the claim ledger substrate** |
| Typed edges with confidence + source passage | **built** (`upsert_edge`, `citations`) | **the argument graph substrate** |
| Extraction schemas (YAML) + cached LLM extraction | **built**; `claims.yaml`, `conceptual_overlap.yaml` exist | claim *nomination* at scale |
| `search_sources` → `ingest_execute` | **built** | **Workstream 1B, the snowball** |
| Citation edges (`cites`) from the acad pack | **built** | forward/backward snowballing |
| Filter extensions (plugin-defined search filters) | **built** | `status=open`, `public_ready=true` filters |
| Bibliographic records / CSL / work-vs-edition | **not built** (P3) | ledger's "exact source, edition, pp." |
| Projects, saved queries, screening | **not built** (P4) | scoping the corpus to one brick |
| Claim embedding layer, `supports`/`contradicts` | **not built** (P5) | duplicate-premise detection at scale |

Two things to take from this table. First, the substrate for the ledger and the graph
is **already there** — you are not waiting on a subsystem. Second, the three unbuilt
pieces (P3, P4, P5) are precisely the three the program will eventually want, and P3
is the one that bites first.

Also already in the corpus and directly relevant: **TDNT (25,852 passages)** plus the
Logos library. HALOT is ingested but known-truncated (see the Logos chunker defect
note) — verify before leaning on it for a `deror` argument.

---

## 2. The central design decision: the ledger is its own tables

An earlier draft of this document proposed modelling a claim as an entity
(`entity_type='claim'`) and its evidence as `mentions`, on the grounds that it needs
no migration. **That is wrong, and the reason is worth stating precisely.**

`mentions.passage_id` is `ON DELETE CASCADE`. `edges.source_passage_id` is
`SET NULL`. Passages are deleted and reinserted by `reindex chunks`. So a ledger
anchored to passages loses its entire evidence layer the first time the corpus is
re-chunked, and nulls the provenance of every proponent edge — silently, with no
error. `research-workflow-gaps.md` §1.2 names this exact hazard and warns that "any
design that adds more passage-anchored data makes this worse." A claim ledger is the
most passage-anchored data imaginable.

The right shape is the one the FK instinct suggests: **the research output is its own
tables in the same database, with real foreign keys into the corpus.** Three tables.

### The anchoring rule

> **Anchor to `(document_id, char_start, char_end)`. Never to `passage_id`.**

Passages are *derived* — one chunking of a document among several possible ones,
versioned and disposable. `document_texts.text` is the substrate they all address,
and it is stable. Since P1 landed, `passages.char_start`/`char_end` are real indexed
columns into that text, with `passages_doc_span_idx` on
`(document_id, char_start, char_end)`. So the passage is always *recoverable* from an
offset anchor by index scan, while the anchor itself survives re-chunking, re-parsing,
and re-embedding untouched.

`verify_quote` already returns exactly this anchor — a span into canonical text, with
`straddles_passages` when the quote crosses a chunk boundary (which quotations
routinely do, and which is precisely the case where no passage matches at all).
So the write path is: verify the quote, store what verification returned.

### Schema

```sql
CREATE SCHEMA argument;

CREATE TABLE argument.claims (
    id                 uuid PRIMARY KEY,
    ref                text NOT NULL UNIQUE,          -- 'JUB-004', the handle you cite
    statement          text NOT NULL,                 -- the proposition, one sentence
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

-- The argument graph. Real FKs on both ends, which `core.edges` cannot give you:
-- its (kind, id) pairs are polymorphic and therefore unconstrained.
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

-- Evidence. One row = "this text, in this document, at these characters,
-- plays this role for this claim."
CREATE TABLE argument.anchors (
    id               uuid PRIMARY KEY,
    claim_id         uuid NOT NULL REFERENCES argument.claims(id) ON DELETE CASCADE,
    role             text NOT NULL,   -- asserts | supports | rebuts | context
    person_entity_id uuid REFERENCES core.entities(id) ON DELETE SET NULL,

    -- The durable anchor. RESTRICT: you may not delete a document your ledger cites.
    document_id      uuid NOT NULL REFERENCES core.documents(id) ON DELETE RESTRICT,
    char_start       integer NOT NULL,
    char_end         integer NOT NULL,
    quoted_text      text NOT NULL,

    -- Verification provenance. `parser_version` mirrors document_texts: if the
    -- parser moves, offsets may have moved with it and the anchor needs re-checking.
    verify_status    text,            -- exact | normalized | near
    verified_at      timestamptz,
    parser_version   text,

    -- Citation identity, until P3 lands. Then zotero_key becomes the join.
    edition          text,
    zotero_key       text,
    locator          jsonb NOT NULL DEFAULT '{}',

    -- Convenience cache only. Never authoritative; recoverable by span overlap.
    passage_id       uuid REFERENCES core.passages(id) ON DELETE SET NULL,

    created_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT anchors_span_ck CHECK (char_end > char_start)
);
CREATE INDEX anchors_claim_idx    ON argument.anchors (claim_id);
CREATE INDEX anchors_doc_span_idx ON argument.anchors (document_id, char_start, char_end);
```

Three constraints in there are doing real work:

- **`anchors.document_id ... ON DELETE RESTRICT`** — the corpus can no longer drop a
  source the ledger depends on. `mentions` could never give you this.
- **`claim_edges.target_id ... ON DELETE RESTRICT`** — you cannot delete a claim other
  claims rest on. The graph refuses to silently lose a load-bearing premise.
- **`claims.ref UNIQUE`** — `JUB-004` means one thing across the dossier, the essay,
  the video script, and the slide deck.

### Recovering the passage from an anchor

```sql
SELECT p.id, p.text
FROM core.passages p
WHERE p.document_id = :document_id
  AND p.char_start  <  :char_end
  AND p.char_end    >  :char_start
  AND p.chunker_version = :current_version;
```

Index scan on `passages_doc_span_idx`. Run it at read time; store the result in
`anchors.passage_id` if you want, and let it go stale without consequence.

### Where these tables live

The manifest has no table or migration contribution — `PluginContributions` covers
document/entity/event/relation types, chunkers, ingestion modules, extraction schemas,
MCP tools, hooks, vocabularies, filter extensions, and source search, but nothing
owning DDL. The `acad` pack works around this by managing its own tables through its
own asyncpg pool, out of band from alembic and invisible to `doctor`.

Don't imitate that here. **Put these three tables in core as migration 009.** Two
reasons: cross-schema FKs into `core.*` are exactly the coupling that makes an
out-of-band pack schema fragile, and this *is* the generic object §5 of the gaps doc
recommended building in core (Option C, the project primitive) — arriving early
because the research needs it.

What stays in a pack is the domain vocabulary, which the manifest already supports
well: `relation_types` with inverses (`depends_on`/`supports`, `rebuts`/`rebutted_by`),
the `entity_types` for `lexeme` and `scripture_ref`, the redistribution extraction
schema, and the opinionated MCP tools.

### What this gives up, and the fix

Entities carry `entity_aliases`, and `resolve_entity` does fuzzy matching over them —
that was the free duplicate-premise detector from Workstream 1B. Own tables lose it.
Add a fourth table when it starts to matter:

```sql
CREATE TABLE argument.claim_phrasings (
    claim_id uuid NOT NULL REFERENCES argument.claims(id) ON DELETE CASCADE,
    phrasing text NOT NULL,
    PRIMARY KEY (claim_id, phrasing)
);
```

with a trigram index, and later the P5 claim-embedding layer clustering over it. That
is a better duplicate detector than entity aliasing anyway, because it can cluster on
meaning rather than name.

Scholars stay `core.entities` of type `person` — that is what entities are for, and it
keeps `resolve_entity` returning people rather than a mixed bag of people and
propositions.

---

## 3. The leverage query

This is the artifact that justifies the whole approach. Transitive dependents of a
claim, over `depends_on`:

```sql
WITH RECURSIVE downstream AS (
    SELECT e.source_id AS claim_id, 1 AS depth
    FROM argument.claim_edges e
    WHERE e.relation = 'depends_on' AND e.target_id = :claim_id
  UNION
    SELECT e.source_id, d.depth + 1
    FROM argument.claim_edges e
    JOIN downstream d ON e.target_id = d.claim_id
    WHERE e.relation = 'depends_on' AND d.depth < 12
)
SELECT c.ref, c.statement, min(d.depth) AS depth
FROM downstream d
JOIN argument.claims c ON c.id = d.claim_id
GROUP BY 1, 2
ORDER BY depth;
```

`UNION` rather than `UNION ALL` terminates on cycles, which a real argument graph
will contain — two authors can each treat the other's conclusion as a premise, and
you want that visible rather than fatal.

Ranked across every open claim, that is **where next week goes**. It is the thing a
Markdown document cannot tell you, and the reason a narrow lexical claim about one
Hebrew term can be worth more than a book-length rebuttal.

Because both endpoints are real foreign keys, the count is trustworthy: there is no
such thing as an edge pointing at a claim that does not exist. On `core.edges`, whose
`(kind, id)` pairs are polymorphic and unconstrained, it would not have been.

---

## 4. What to build — one migration, four tools, pulled by real work

Migration 009 creates the three tables (§2). A `ClaimRepo` in
`adapters/storage/postgres/repositories/claims.py` and core MCP tools expose them —
same container, same MCP surface as `find_passages` and `verify_quote`, which is the
whole point of keeping the research in this database.

The domain vocabulary goes in a pack at `packages/plugins/argument/` **in this repo**,
alongside `history/`, installed with `--link`. Not a standalone repo: the last time a
pack got its own repository it was reverted the same day.

Build in this order, and **only when a real brick demands it**:

1. **`claim_upsert`** — one call writes the claim, its `depends_on` edges, and its
   anchors. Crucially it should **call `verify_quote` itself** and refuse to store an
   anchor that comes back `not_found`, persisting the returned span, status, and
   `parser_version`. An unverifiable quotation must not be able to enter the ledger.
2. **`argument_graph`** — traverse from a claim: ancestors, descendants, proponents,
   rebuttals. The core `citations` tool only hydrates *document* endpoints, so it
   cannot walk this graph.
3. **`claim_leverage`** — the query in §3, ranked.
4. **`claim_reverify`** — re-run `verify_quote` over every anchor whose stored
   `parser_version` no longer matches its document's. This is the maintenance tool that
   keeps a four-year-old ledger honest across re-parses, and it costs about thirty
   lines.

Then, later and only if the corpus outgrows hand-curation:

5. A `redistribution_claim` extraction schema — modelled on `conceptual_overlap.yaml`,
   which is already aimed at this exact subject matter (property rights, government
   coercion, Christian moral tradition, natural law). Use it to **nominate** candidate
   claims from newly ingested opposition texts; promote to canonical claims by hand.
   Never let an LLM write a ledger row directly — the S4 extraction-quality spike was
   never run, and a ledger you don't trust is worse than no ledger.
6. A `ClaimStatusFilter` filter extension so `find_passages` can be scoped to passages
   bearing on open claims — a subquery returning `passage_ids` by span overlap against
   `argument.anchors`.

**Budget: two weeks of build, maximum, inside the twelve.** The failure mode here is
obvious and fatal — building a beautiful claim engine instead of reading Sider.

---

## 5. The one real dependency: citation identity (P3)

The ledger's "exact source — book, edition, pp. xx–yy" is the field the whole project's
credibility rests on, and it is the field the engine currently supports worst.
There are no bibliographic records, no work-vs-edition identity, and no CSL output.
Locators exist and `verify_quote` returns them, so you can cite a page — but *which
edition's* page is metadata nobody stores.

This matters concretely: Sider's *Rich Christians* ran to six editions across forty
years and the argument moved between them. "Sider says X (p. 214)" is not a checkable
claim without an edition.

**Do not build P3 now.** Instead:

- Keep Zotero as the bibliographic authority for the seed corpus (your Week 1–2 item —
  it was the right call).
- Record `attributes.zotero_key` on each source document at ingest, and
  `attributes.edition` free-text on the claim's `asserts` edges.
- When P3 lands, that key is the join and nothing is lost.

Zotero and this engine are not competitors: Zotero owns *identity and output format*,
the engine owns *full text, retrieval, and the graph*. One stable key bridges them.

---

## 6. What must not go in the engine

- **Drafts, essays, scripts, shorts outlines, the book file.** Files, not rows. Link
  them to claims by `claim_id` in front-matter, so the rebuttal library (Workstream 5)
  is *derivable* — "give me every asset for JUB-004" is a grep on claim ids.
- **The content calendar and the ideas backlog.** Not research data.
- **Current-events reactions.** Workstream 7's discipline enforces itself if nothing
  in the pipeline can hold a dated take.

And a licensing constraint worth writing down once: Logos, Kindle, and YourCloudLibrary
ingest **licensed** text. Quoting a paragraph in a video with attribution is ordinary
scholarly use; exporting passages wholesale into a public artifact is not. Since the
whole point of this program is public output, keep that boundary explicit rather than
implicit in a README.

---

## 7. Revised twelve weeks

Your sequencing put infrastructure in Weeks 1–2 and opposition reading in 3–6. Against
this codebase that inverts badly: you would spend the first fortnight writing Python.
Let one real brick pull the tooling into existence.

**Week 1 — spike, don't build.** Take *one* claim end to end with tools that already
exist and no new code (see §8). Ingest a **narrow** seed: 20–40 works, not the library.
Canonical text and the current chunker are only needed for the works you will actually
cite, so this is days, not a re-ingest of 2,728 documents.

Seed corpus: Sider, Wolterstorff, Mott, Brueggemann, Yoder, Rae, Wright where relevant;
CST primary texts (*Rerum Novarum*, *Quadragesimo Anno*, *Laborem Exercens*,
*Centesimus Annus*, the *Compendium*) — all freely available, so `search_sources` should
find them ingestable rather than borrowable. Lexical: TDNT (already in), BDB, NIDOTTE,
HALOT (**verify the truncation first**).

**Week 2 — migration 009 + `claim_upsert`.** The three tables and one tool. Then
backfill the claims already sitting in your master document; twenty real rows is the
only honest test of whether the schema fits, and it is cheap to change in week two and
expensive in week nine.

**Weeks 3–6 — Sider, deep.** Read to defend. Extract claims by hand into the ledger.
Snowball with `citations` (backward, via the acad pack's `cites` edges) and
`search_sources` → `ingest_execute` (forward, into the corpus). Build `argument_graph`
when you first need to see a subtree, and `claim_leverage` when the graph passes
~60 claims and you can no longer eyeball it.

**Weeks 7–10 — the Jubilee brick.** Now the graph earns out: run `claim_leverage`
across the Jubilee subtree *before* deciding what to write. If restoration-vs-
redistribution turns out to carry seven dependents and the *deror* lexical claim carries
two, that ordering is the finding, and it decides the paper.

**Weeks 11–12 — public translation.** Unchanged from your plan. The one addition:
`public_ready=true` becomes a query, so the content backlog is generated, never
maintained.

**Weeks 13+ — nothing scheduled.** The unbuilt subsystems (P3, P4, P5) and the rules
engine all wait on triggers rather than dates; see the deferral schedule in §10. The
single exception is the integrity rules, which are worth writing the week the ledger
exists.

---

## 8. Day one: the smallest thing that proves the design

Pick a claim you already believe you understand — Jubilee as periodic redistribution.
With existing tools only:

1. `search_sources` for Sider's *Rich Christians*; `ingest_execute` the match.
2. `find_passages` for the Jubilee argument; read the hits with `get_passage_context`.
3. `verify_quote` the sentence you would put in a script. Note the locator it returns.
4. Write down, by hand in a scratch file: the claim `JUB-004`, the premise it rests
   on (`JUB-001`), Sider as its proponent, and the span from step 3 — `document_id`,
   `char_start`, `char_end`, `quoted_text`, the verify status.

That is one row per table. You are not building anything yet; you are checking that
`verify_quote` gives you every column `argument.anchors` declares, for a real book,
without a single field you have to invent. If it does, migration 009 is a transcription
job. If it doesn't, you have found the schema defect before it has 200 rows in it —
which is the entire purpose of doing this by hand first.

Do it for three claims, not one. The second and third are where the fields you forgot
show up.

---

## 9. Risks, named

- **Tool-building as procrastination.** The most likely failure. Your own rhythm says
  60% deep research; hold the pack to two weeks.
- **LLM-extracted claims polluting the ledger.** Extraction nominates; you promote.
  The extraction-quality spike (S4) has never been run.
- **Edition drift.** See §5. Fill `anchors.edition` from day one even though nothing
  validates it; backfilling editions across 200 anchors is a different project.
- **Re-parsing, not re-chunking, is now the anchor hazard.** Offsets survive
  re-chunking by construction, but a docling upgrade can shift canonical text under
  them. That is why `anchors.parser_version` exists and why `claim_reverify` is tool 4
  rather than a someday item.
- **Snowball explosion.** `search_sources` + `ingest_execute` makes ingesting a
  bibliography almost free, which means the corpus can outrun your reading by an order
  of magnitude. Ingest what a claim needs, not what a search returns.
- **HALOT is truncated.** A `deror` argument built on it is built on sand until the
  Logos chunker defect is confirmed fixed.

---

## 10. The final vision — the system at maturity

Everything above is the next twelve weeks. This section is the endpoint the twelve
weeks should be compatible with, so that nothing built early has to be torn out. None
of it is active work. Each piece has a **trigger**, not a date.

### The layered picture

```
  Layer 4   OUTPUTS      dossier · paper · essay · video · short · deck · FAQ
                         files, outside the engine, each stamped with claim refs
                              ↑  generated from, never hand-synced
  Layer 3   REASONER     integrity · propagation · argumentation semantics
                         separate process; writes derivations with justifications
                              ↑  reads the fact base, writes conclusions back
  Layer 2   LEDGER       claims · claim_edges · anchors · derivations
                         core, migration 009
                              ↑  anchored by (document_id, char_start, char_end)
  Layer 1   CORPUS       documents · document_texts · nodes · passages · embeddings
                         built
                              ↑  search_sources → ingest_execute
  Layer 0   SOURCES      Logos · YourCloudLibrary · journals · CST · lexica
```

Each layer depends only downward, and each arrow is a contract that can hold still
while the layer above it churns. That is the whole architectural claim: **rules change
weekly, outputs change monthly, the ledger changes quarterly, the corpus schema barely
changes at all** — and nothing forces a fast-moving layer's churn onto a slow one.

### The property worth designing for

> **Every public sentence traces down to character offsets in a source.**

A video script cites `JUB-004`. `JUB-004` has anchors. Each anchor is a verified span
in `document_texts`, in a named edition, with a locator. A hostile reader can descend
from a YouTube short to the exact characters of Sider's page and check them.

That is a stronger portability guarantee than a PDF bibliography (Workstream 9), and it
is what makes the Workstream 10 question — *"have I stated your argument correctly?"* —
specific and answerable rather than rhetorical.

### Layer 3, the reasoner — deferred, but shaped now

The full treatment is in §11. What matters for the vision:

Rules run on **graph structure and status metadata, never on propositional content**.
An interpretive claim about Leviticus will not formalize into predicates; *"this claim
is `public_ready` and transitively depends on a claim still `open`"* formalizes in one
line and is the thing you actually need enforced.

At maturity the reasoner does three things: checks the ledger is well-formed, propagates
status changes so that refuting a premise marks its dependents suspect, and computes
which of your rebuttals survive mutual attack. The durable output is a `derivations` row
carrying its **justification** — which rule fired on which facts — because an inference
you cannot audit is worse than no inference in a project whose credibility rests on
being checkable.

Two consequences reach back into the present: `argument.derivations` should exist as
soon as the ledger does (retrofitting justifications onto rules written without them
never happens), and `claim_edges.note` should be filled in from the first edge, because
rules amplify sloppy `depends_on` links into confident wrong propagation.

### What each workstream draws from the finished system

- **WS1 (opposition map):** the map is `argument_graph` over `entity_type='person'`
  proponents, not a document. New authors join it by ingest, not by rewrite.
- **WS2 (claim ledger):** the ledger *is* layer 2. Its lifecycle column becomes a
  status that layer 3 propagates rather than a field you remember to update.
- **WS3 (argument graph):** layer 2's edges, rendered. The leverage ranking decides
  research order, computed over live dependents once statuses propagate.
- **WS5 (rebuttal library):** each asset carries claim refs; the library is a view.
- **WS8 (conferences):** a graph subtree export *is* the one-page argument map, and the
  documents behind its anchors *are* the annotated bibliography.
- **WS9 (portability):** the property above.
- **WS10 (external criticism):** send a critic `claims.steelman` plus the `asserts`
  anchors with their verified quotations — a specific question about specific rows,
  each carrying the exact characters it rests on.
- **WS11 (the book):** activation becomes countable rather than felt —
  `status='rebutted' AND public_ready AND NOT unsafe`, count ≥ 5, having survived
  external criticism. A query, evaluated whenever you wonder.

### The deferral schedule

Ordered by trigger, not date. Nothing here is scheduled; each becomes obvious when its
trigger fires.

| Deferred | Trigger | Why wait |
|---|---|---|
| Integrity rules (§11 family 1) | the ledger exists | a few CTEs; catches data-entry damage while repair is cheap — **the one exception, build it early** |
| `argument.derivations` table | the ledger exists | cheap now, unretrofittable later |
| **P3** — bibliographic records, CSL, work-vs-edition | first academic submission | Zotero + `zotero_key` covers it until something must be formatted |
| Contradiction detection among your own claims | claims span two or more bricks | the sleeper feature; an opponent finding your inconsistency is far worse than you finding it |
| **P4** — projects, saved queries, screening | a second brick runs concurrently | corpus-wide search stops being useful only when two questions compete |
| Straw-man rule (§12) | the ledger exists | one CTE, and it guards the thing the project is most vulnerable on — **build it early** |
| Scheme vocabulary + critical questions (§12) | ~80 claims | classifying claims teaches you nothing until there are enough to classify |
| Propagation rules (§11 family 2) | ~80 claims | below that every rule fires on what one screen already shows |
| **P5** — claim embeddings, `contradicts` edges | hand-curation breaks, ~300 claims | later than it feels |
| Argumentation semantics (§11 family 3) | mutual attacks you cannot hold in your head | grounded/preferred only matters once positions genuinely conflict |

### What "done" looks like

Not a finished book, and not a complete opposition map — neither ever finishes. The
system is working when three things are true at once:

1. **Research order is computed, not chosen.** You open `claim_leverage`, and the top
   row is next week's work.
2. **Publication is gated, not remembered.** Nothing reaches a script while resting on
   an open premise, because the gate is a rule rather than your discipline.
3. **Every claim is descendable.** Any sentence you have said in public can be walked
   down to the characters of the source it rests on, by someone who wants you to be
   wrong.

At that point the book is not a writing project. It is a serialization of layer 2.

---

## 11. Appendix — the production system in detail

**Deferred.** Retained here because the shape of layer 3 constrains two decisions in
the present (see §10), and because the reasoning behind the deferral is worth keeping.

Naming this a *production system* rather than a library feature is the load-bearing
observation: it is a different kind of system, with a different lifecycle, and the
architecture should say so.

### The line that keeps it tractable

> **Rules operate on graph structure and status metadata. Never on the propositional
> content of a claim.**

"Jubilee restored hereditary allotments rather than reallocating by need" will not
usefully formalize into predicates — it is an interpretive claim about an ancient text
and its content is irreducibly natural language. But *"this claim is marked
`public_ready` and transitively depends on a claim whose status is still `open`"* is a
purely structural fact, and it is the one you actually need enforced.

Everything valuable here is on the structural side. Attempts to formalize the content
are where projects like this die.

### Three families of rule, in increasing ambition

**1. Integrity — is the ledger well-formed?**

- a claim with no anchors → an assertion with no evidence
- a `depends_on` cycle → circular support
- a claim of kind `mine` that rebuts nothing → orphan objection
- an anchor whose `parser_version` has drifted from its document's → §4 tool 4

**2. Propagation — what follows from a status change?**

This family pays for the whole thing. When you refute a premise, every claim resting on
it becomes *suspect* — not refuted, since the conclusion may be true on other grounds,
but no longer supported by that route.

```prolog
undermined(C) :- depends_on(C, P), status(P, refuted).
undermined(C) :- depends_on(C, P), undermined(P).

unsound(C)    :- depends_on(C, P), status(P, open).
unsound(C)    :- depends_on(C, P), unsound(P).

unsafe(C)     :- public_ready(C), undermined(C).
unsafe(C)     :- public_ready(C), unsound(C).
```

Six lines. The last two mechanize the discipline from Workstream 2 — *"only then mark
it ready for publication"* — which is otherwise a habit you must sustain across years
and hundreds of claims. As a rule it is enforced, and it re-fires the moment a premise
regresses. That is the difference between a checklist and a system.

**3. Defeasible reasoning — which of my rebuttals actually survive?**

Your `rebuts` edge is an *attack relation*, and "which claims are jointly defensible,
given who attacks whom" is a solved problem with a thirty-year literature:
**abstract argumentation** (Dung 1995) and its structured descendants (ASPIC+,
Carneades). Your graph is already most of the input format.

The output is an *extension* — a set of claims that collectively defend themselves
against all attackers. Two semantics matter, and the choice is not cosmetic:

- **Grounded** — maximally skeptical, always unique. What survives no matter what.
- **Preferred** — credulous, possibly several. What you *could* coherently defend.

Publish from the grounded extension; explore with the preferred ones. "Team Jesus
publishes only what survives the skeptical semantics" is a defensible editorial
policy, and an unusually honest one to be able to state in public.

### Where it lives — you are right that this is not the library

A production system does not belong inside the corpus engine:

1. **Rules change weekly; the corpus schema must not.** Coupling them means schema
   churn driven by research whims.
2. **Reproducibility.** A rule run should be a dated artifact — *given these 214
   claims, these 96 edges and these 31 rules, on 2026-09-01, these 7 claims are unsafe
   to publish.* That is citable and re-runnable. Triggers firing invisibly inside
   transactions are not.
3. **Testability.** Rules over an exported fact base are unit-testable against
   fixtures. Rules as database triggers are testable only against a live database.

So the seam is: **`argument.*` is the fact base; a separate reasoner reads it, runs
rules, and writes conclusions back with their justifications.**

```
argument.claims      ──┐
argument.claim_edges ──┼──→  export facts  ──→  solver  ──→  argument.derivations
argument.anchors     ──┘         (read-only)                  (run_id, rule,
                                                               conclusion,
                                                               justification)
```

`derivations` is the important table and `justification` is the important column: it
records which rule fired on which facts. **An inference you cannot audit is worse than
no inference**, particularly for a project whose entire credibility rests on "he
accurately understands our position." You must always be able to answer *"why does the
system say JUB-004 is undermined"* with a chain, not a verdict.

Note that this also makes the §3 leverage metric more honest. Raw dependent-count
treats a conceded claim as load-bearing; once statuses propagate, leverage can be
computed over live dependents only.

### Tooling

Start with **recursive CTEs**. The §3 leverage query is already one inference rule;
integrity and propagation are the same shape, and they cost no new dependency. A view
per rule, materialized on demand, gets you families 1 and 2 entirely.

Move to **clingo (Answer Set Programming)** when you need negation-as-failure or
multiple extensions — which is exactly when family 3 arrives, because "these two
positions are each internally coherent and mutually incompatible" has more than one
answer and SQL has no way to express that. clingo has a solid Python API, is actively
maintained, and published encodings of Dung semantics run to about a dozen lines.

**Do not use an LLM as the inference engine.** Your gaps doc §7 already rejected this
for synthesis, and the reason applies with more force here: "two runs give two answers,
which disqualifies it for citation." The LLM's job is to *propose* edges from text; the
solver's job is to draw conclusions from edges. Keep those separate.

### The honest limits

- **Rules cannot tell you a claim is true.** They tell you whether your position is
  internally coherent and properly grounded. Different properties — but the second is
  the one that can be mechanized, and it is the one you would otherwise lose track of.
- **Garbage in, amplified out.** Sloppy `depends_on` edges produce confident wrong
  propagation. Rules raise the cost of careless edges considerably, which is an
  argument for drawing them slowly and for actually filling in `claim_edges.note`.
- **The self-contradiction check is the sleeper feature.** Across four years and three
  hundred claims you *will* rely on a reading in one brick that you attacked in
  another. An opponent finding that is far worse than you finding it. Detecting
  contradictions among your own claims is worth building before status propagation is.

### When to build it

See the deferral schedule in §10. In short: family 1 the week the ledger exists;
families 2 and 3 once the graph is large enough that a rule can tell you something a
single screen cannot.


---

## 12. Grounding the checks in a logic text

### The objection this has to answer

§11 draws a hard line: rules operate on structure, never on propositional content. A
fallacy checker looks like exactly that violation — "is this a straw man?" is a
question about what the argument *says*.

The resolution is not to relax the line. It is to notice that **fallacies are not
detected; argument schemes are classified, and their critical questions are counted.**

### Schemes and critical questions

Don't ask "is this an appeal to authority?" — that is a judgment, and a model asked to
make it will answer plausibly and unreliably. Ask instead: *what form of argument is
this, and which of the questions that form is known to invite have I not yet answered?*

Douglas Walton's *Argumentation Schemes* is the mature version of this: roughly sixty
defeasible inference patterns, each with an attached list of **critical questions**.
Classification is one judgment per claim, made once by you. Everything after it is pure
structure:

```prolog
unanswered(C, Q)   :- uses_scheme(C, S), critical_question(S, Q), not answered(C, Q).
incomplete(C)      :- unanswered(C, _).
not_publishable(C) :- public_ready(C), incomplete(C).
```

An unanswered critical question is **a hole, not a verdict**. That distinction is the
whole design. The system never says an argument is fallacious — usually undecidable,
and often false, since argument from authority is frequently perfectly good. It says
only that a question the form invites has not been addressed yet.

This lands in the same territory as §11 family 1: a completeness check over structure,
not an opinion about content.

### The one fallacy worth enforcing structurally

Straw-manning is the failure mode that kills polemical projects, and it is the one you
*can* catch mechanically — because it is a question about provenance, not content.

> **You may not `rebut` a claim you paraphrased. Only one you anchored.**

```prolog
verified_assertion(T) :- anchor(T, A), role(A, asserts),
                         verify_status(A, S), S != near.
strawman_risk(C)      :- rebuts(C, T), not verified_assertion(T).
```

If the target of your rebuttal carries no `exact` or `normalized` anchor from a
proponent, you are attacking your own restatement of them. This is Workstream 10's
question — *"have I stated your argument correctly?"* — enforced at write time instead
of asked at review time, and it is the single highest-value rule in the whole system
for a project of this kind.

It needs no new tables. `anchors.role` and `anchors.verify_status` from §2 already
carry everything it reads.

### Where the book goes — two places, doing different jobs

1. **The corpus (layer 1).** Ingest it. It is a source like any other, and once
   ingested its taxonomy has anchors.
2. **The vocabulary (layer 3's input).** The manifest already supports
   `vocabularies: [{id, file}]`. The `argument` pack ships `schemes.yaml` — each scheme
   with its id, its form, its critical questions, and **an anchor back into the book**.

That second point closes a loop worth closing. When a critic asks why the system
demanded you answer a particular question, the answer is a page reference rather than
"because I coded it that way." For a project whose credibility rests on being
checkable, having the *method* anchored the same way the *claims* are is not a small
thing.

### Which book changes the design materially

- **Walton, *Argumentation Schemes* / *Fundamentals of Critical Argumentation*** —
  best case by a distance. Schemes and critical questions arrive pre-formalized, and
  Walton is also the bridge to §11 family 3: his rebutter/undercutter distinction is
  the attack relation Dung semantics operate on. One book grounds both layers.
- **Damer, *Attacking Faulty Reasoning*** — organizes fallacies as violations of five
  criteria of a good argument (structural, relevance, acceptability, sufficiency,
  rebuttal). Five check families rather than sixty schemes: coarser, very usable, and
  its rebuttal criterion is essentially the straw-man rule above.
- **Copi & Cohen, or Hurley** — the formal-logic half will not apply, since your claims
  are interpretive rather than propositional. The informal-fallacy chapters give a
  taxonomy but no critical questions, so you would author those yourself.
- **A popular fallacy catalogue** — weakest as a rule source, though genuinely useful
  for Lane B public content.

### Limits

- **Scheme classification is a judgment.** A model will nominate a scheme fluently and
  wrongly. Same discipline as claim extraction: the model proposes, you confirm. A
  misclassified scheme attaches the wrong critical questions and yields confident
  nonsense.
- **Do not build a general fallacy detector.** Prose scanned for fallacies is a demo.
  The completeness check over schemes you deliberately classified, plus the one
  structural straw-man rule, is a system.
