# Decision 002 — The contradiction finder's structure is core; its traditions are a pack

**Status:** accepted, 2026-10-13; amended the same day by
[Decision 003](decision-003-dilemma-finder-v2.md)
**Applies to:** migration `021_provenance` and the `synthesis` migrations that
follow it (`022`–`024`), the SDK `BibliographyClient`, and the in-repo
`dilemma` pack.

## The question

The Dilemma Finder mines a tradition's binding corpus for internal
contradictions and escalates the strongest into dilemmas: two horns, a text
vouching for the other's authority, and the tradition's standard escape
hatches, each open or closed by a text. It needs a source registry with
licences and a scan queue, authority that changes over time, cross-edition
alignment, a claim layer with embeddings, a funnel and human review. All of it
is new, and all of it is about Mormon and Islamic texts.

The pack rule (`corpus-engine-docs/docs/07-pack-system.md`) says core holds
only what a librarian of family recipes would want, and "when in doubt, put it
in a pack". Decision 001 refined that: core owns structure, the pack owns what
it means. The question is where that line falls here.

## The decision

Draw the line on vocabulary, not on subject matter. Every table is
domain-neutral and lives in core. Every word that names a tradition — `lds`,
`canon`, rank 1, `vouches_for`, a hatch key — is data the pack supplies, and
core validates none of it with a CHECK.

| Core owns | The `dilemma` pack owns |
|---|---|
| `bibliography.acquisitions`, `artifacts`, `coverage_reports` — where an edition came from, on what terms, and whether it loaded whole | the manifest of 120 editions, families A–H, and what each family's agent must verify |
| `bibliography.edition_authority` — who held an edition authoritative, over which dates, for which part | the tiers per community and the canonization dates |
| `core.document_marks` — ranges the node tree cannot hold | that a KJV italic is `supplied` and an 1830 page break is `page` |
| alignments and word diffs (`022`) | the Book of Mormon / Bible parallels to align |
| claims anchored on spans, source-to-source relations, the funnel and review (`023`, `024`) | the escape-hatch catalogue, the topic taxonomy, the extraction prompt |

Each core shape has a second, non-religious reader, which is what the recipe
test asks for in substance:

- **Acquisitions.** Any library has books it holds and books it wants.
- **Edition authority.** It is "statute in force" and "binding vs persuasive
  precedent".
- **Marks.** Any scanned book has page breaks.
- **Alignments.** Every multi-edition text needs them, and WLC↔LHB already
  needs them now; its divergence is computed ad hoc.
- **The claim layer.** It is the design `docs/design/research-workflow-gaps.md`
  §7 already recommended for contradiction detection across the whole corpus.

## Why not the pack

1. **Every table needs a foreign key into core.** Claims anchor on
   `evidence.source_spans`; authority scopes to a span; marks address
   `core.document_texts`. Decision 001 already found that a pack-owned table
   cannot hold that relationship. It lives outside Alembic,
   `test_schema_truthfulness`, `CorpusFootprint` and the backups.
2. **Durable rows cannot key on passages.** `reindex chunks` deletes and
   re-inserts `core.passages`. Anything a person reviews — a claim, a verdict,
   a standing — anchors on spans, which only core can protect with RESTRICT.
3. **The pack still owns everything that is a judgement.** Which editions
   matter, what counts as canon and when, which hatches a tradition reaches
   for. All of it is YAML a reader can argue with, validated on load.

## Deviations from the spec, recorded

The spec this was built from is the operator's. These are the places the build
departs from it, and why:

- **Spec "passage" ≠ repo passage.** A spec passage is a verse/page node plus
  its span. Nothing durable keys on `core.passages`.
- **Horns are not only T1–T3.** A text outside the binding set can still be
  a horn when a binding text vouches for it: it is horn-eligible at its
  voucher's rank.
- **The blind validation withholds seeds.**
  - Any seed that names the answer a blind run must find is kept out of it.
  - The blind run blocks on nothing and passes at pair level after Stage 2,
    before any LLM sees both horns.
  - The adjudicating model is not blind, and the write-up says so.
- **Extraction is gated by cost and scale.** Ranks 1–2 are extracted in full.
  Ranks 3–5 are extracted only for units the taxonomy's topic queries
  retrieve. Each work is extracted once and carried across its editions by
  alignment, except where a diff is doctrinal.
- **Arabic binds; the models read English.** Claims anchor on the translation
  and carry the ayah-aligned Arabic span.
- **Docs are split by kind.**
  - Plans live in PR descriptions.
  - Decisions live here.
  - Generated reports go to `~/.research-engine/plugin-data/dilemma/reports/`.
    They quote copyrighted text, so they never enter the repository.

## What this does *not* license

- **No tradition in core code.** If a core service ever branches on `lds` or
  `islam`, the line has been crossed. Move the branch into pack data.
- **No pack DDL.** The `dilemma` pack declares no `database` contribution. A
  table it seems to need is either a generic shape for core, with a second
  reader named, or it is not needed.
- **No licence by default.** A manifest licence is `unverified`, a known
  public-domain or copyright status to confirm, or private use. Never a guess.

## When to revisit

- When a third tradition arrives, check that it can be added as data alone.
- When a core table grows a column only one tradition fills: per decision
  001, that column becomes a `jsonb` attribute rather than accumulating one
  column per tradition.
