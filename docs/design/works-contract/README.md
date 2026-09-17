# Works — created output, linked to the corpus

This directory holds **created works**: translations, essays, dossiers, video
scripts, outlines — Layer 4 output in the terms of
`docs/design/research-program-on-marginalia.md`.

The rule from that doc (§6), as staged by the controlling architecture below:
**works are files until their first freeze**, then rows in `authored.*`. The engine
keeps claims, evidence, and the edges between them; a work is a rendering of that.
But a work is not free text. Every citation in a work is a **structured span
citation** — a `document_id` plus character offsets into canonical text — so a
citation is machine-checkable and machine-derivable, never a string you have to
remember what it meant.

Controlling architecture: `docs/design/works-architecture-master.md` (reconciles the
two RFCs; this file contract is the Phase-0 authority until a work's first freeze).
Bridge design: `docs/design/work-citations-architecture.md`.

## The contract

1. **One file per work** under `works/`, kebab-case. Start from `_TEMPLATE.md`.
2. **Every citation is a front-matter entry**, not prose. An entry names the
   document and the exact characters it rests on:

   ```yaml
   citations:
     - id: c1                    # referenced in the body as [^c1]
       document_id: <uuid>       # core.documents.id — required
       char_start: 3326          # offset into document_texts.text — required
       char_end: 3546            # > char_start — required
       quoted_text: "..."        # the span as you quote it — required
       intent: quotation         # required — why it is cited here:
                                 #   quotation | translation | support | contrast
                                 #   | background | definition | source | see_also
       role: supports            # optional — only when this citation also backs a
                                 #   claim ref: asserts | supports | rebuts | context
       edition: "3rd"            # optional until P3 bibliographic records land
       zotero_key: SIDER_2019    # the join once P3 lands; fill it from day one
       locator: {page: 214}      # page / verse / location, whatever the source gives
   ```

   `intent` is the citation's own vocabulary and says why it is here (decision
   11, 2026-09-04). `quotation` and `translation` must verify against the exact
   words; `see_also`, `background` and `contrast` may cite a region; `support`,
   `source` and `definition` are warned when the span is a whole chunk. `role` is
   the claim ledger's vocabulary and belongs on an entry only when the citation
   is also evidence for one of the `claims:` refs.

3. **Span citations verify.** `verify_quote(text, document_id)` returns
   `exact | normalized | near | not_found` plus the span it found. A work is
   review-ready only when every entry verifies `exact` or `normalized`. `near`
   means you edited the quote — re-anchor it. `not_found` means the citation is
   decoration, not evidence; it does not ship.
4. **Bibliographic display is derived, never hand-typed.** Author, title, year
   come from the corpus (`documents.metadata` today; `bibliography.editions`
   once migration 012 lands, extended at P3-1). The front-matter carries only
   identity (`zotero_key`) and location (`edition`, `locator`). A `zotero_key`
   that no ingested document carries is a `work_verify` finding from day one.
5. **Claim refs ride along.** `claims: [JUB-004]` in front-matter — inert until
   migration 009 (`argument.claims`) exists, grep-able immediately. After 009
   the resolver joins them, and "every asset for JUB-004" is a grep today and a
   query later. Same data, two views.

## What this buys

- **Connected citations, not free text.** "Which works cite TDNT entry X?" is
  a grep on `document_id`. "Which works rest on claim JUB-004?" is a grep on
  the ref. After 009 both are SQL with FK integrity behind them.
- **Descent.** Any sentence in a work traces to characters in a source — the
  property §10 of the vision doc designs for ("a hostile reader can descend from
  a published sentence to the exact characters that support it").
- **A clean upgrade path.** Front-matter `zotero_key`/`edition` are exactly the
  columns P3-1 will own; when the ledger and bibliographic tables land, the
  resolver reads the DB and the files do not change.

## What this is not

- Not a database. Do not put structured research data (claims, edges, anchors)
  in these files — that belongs in `argument.*` after 009.
- Not a CMS. Status is `draft | review | published`; publication decisions are
  made by the ledger's rules (§10: "publication is gated, not remembered"), not
  by these files.